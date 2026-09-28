#include "link.h"

#include <mgba/flags.h>
#include <mgba/core/core.h>
#include <mgba/core/lockstep.h>
#include <mgba/core/thread.h>
#include <mgba/gba/interface.h>
#include <mgba/internal/gba/sio/lockstep.h>
#include <mgba-util/audio-buffer.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define PIXELS (240 * 160)
#define AUDIO_FRAMES 2048

struct player {
    struct mAVStream stream;
    struct mCoreThread thread;
    struct mLockstepThreadUser user;
    struct GBASIOLockstepDriver driver;
    pthread_mutex_t mutex;
    atomic_uint keys;
    mColor render[PIXELS];
    mColor video[PIXELS];
    int16_t audio[AUDIO_FRAMES * 2];
    size_t audio_read;
    size_t audio_count;
    uint64_t frame;
    int64_t deadline;
    unsigned audio_rate;
    bool attached;
};

struct gba_link {
    struct GBASIOLockstepCoordinator coordinator;
    struct player players[2];
    bool paused;
};

static int64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t) ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static void frame(struct mCoreThread *thread) {
    struct player *p = thread->userData;
    pthread_mutex_lock(&p->mutex);
    memcpy(p->video, p->render, sizeof(p->video));
    ++p->frame;
    pthread_mutex_unlock(&p->mutex);
    /* Run at GBA speed even if the video stream falls behind. */
    int64_t now = now_ns();
    if (p->deadline < now - 16742706) {
        p->deadline = now;
    }
    p->deadline += 16742706; /* 280896 GBA cycles at 16777216 Hz. */
    struct timespec until = {p->deadline / 1000000000, p->deadline % 1000000000};
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &until, NULL);
}

static void keys_read(void *context) {
    struct player *p = context;
    p->thread.core->setKeys(p->thread.core, atomic_load(&p->keys));
}

static void audio(struct mAVStream *stream, struct mAudioBuffer *buffer) {
    struct player *p = (struct player *) stream;
    int16_t samples[512 * 2];
    size_t count;
    pthread_mutex_lock(&p->mutex);
    while ((count = mAudioBufferRead(buffer, samples, 512))) {
        for (size_t i = 0; i < count; ++i) {
            if (p->audio_count == AUDIO_FRAMES) {
                p->audio_read = (p->audio_read + 1) % AUDIO_FRAMES;
                --p->audio_count;
            }
            size_t pos = (p->audio_read + p->audio_count++) % AUDIO_FRAMES;
            memcpy(&p->audio[pos * 2], &samples[i * 2], 2 * sizeof(int16_t));
        }
    }
    pthread_mutex_unlock(&p->mutex);
}

struct gba_link *gba_link_create(const char *rom, const char *save0, const char *save1) {
    if (!rom) return NULL;
    struct gba_link *link = calloc(1, sizeof(*link));
    if (!link) return NULL;
    const char *saves[2] = {save0, save1};
    GBASIOLockstepCoordinatorInit(&link->coordinator);
    for (int i = 0; i < 2; ++i) {
        pthread_mutex_init(&link->players[i].mutex, NULL);
        atomic_init(&link->players[i].keys, 0);
    }
    link->paused = true;
    for (int i = 0; i < 2; ++i) {
        struct player *p = &link->players[i];
        struct mCore *core = mCoreFind(rom);
        if (!core) goto fail;
        if (core->platform(core) != mPLATFORM_GBA || !core->init(core)) {
            free(core);
            goto fail;
        }
        p->thread.core = core;
        p->thread.userData = p;
        p->thread.frameCallback = frame;
        p->thread.resetCallback = mCoreThreadPauseFromThread;
        mCoreInitConfig(core, "romm-link");
        mCoreConfigSetDefaultIntValue(&core->config, "logLevel", 0);
        mCoreConfigSetDefaultIntValue(&core->config, "useBios", 0);
        mCoreLoadConfig(core);
        core->opts.audioSync = false;
        core->opts.videoSync = false;
        core->setVideoBuffer(core, p->render, 240);
        core->setAudioBufferSize(core, 512);
        p->stream.postAudioBuffer = audio;
        core->setAVStream(core, &p->stream);
        if (!mCoreLoadFile(core, rom)) goto fail;
        if (saves[i] && !mCoreLoadSaveFile(core, saves[i], false)) goto fail;
        struct mCoreCallbacks callbacks = {.context = p, .keysRead = keys_read};
        core->addCoreCallbacks(core, &callbacks);
        if (!mCoreThreadStart(&p->thread)) goto fail;
        mCoreThreadPause(&p->thread);
        p->audio_rate = core->audioSampleRate(core);
    }
    for (int i = 0; i < 2; ++i) {
        struct player *p = &link->players[i];
        mLockstepThreadUserInit(&p->user, &p->thread);
        GBASIOLockstepDriverCreate(&p->driver, &p->user.d);
        GBASIOLockstepCoordinatorAttach(&link->coordinator, &p->driver);
        p->thread.core->setPeripheral(p->thread.core, mPERIPH_GBA_LINK_PORT, &p->driver.d);
        p->attached = true;
    }
    return link;
fail:
    gba_link_destroy(link);
    return NULL;
}

void gba_link_destroy(struct gba_link *link) {
    if (!link) return;
    for (int i = 0; i < 2; ++i) {
        if (link->players[i].thread.impl) mCoreThreadPause(&link->players[i].thread);
    }
    for (int i = 0; i < 2; ++i) {
        struct player *p = &link->players[i];
        if (p->attached) {
            p->thread.core->setPeripheral(p->thread.core, mPERIPH_GBA_LINK_PORT, NULL);
            GBASIOLockstepCoordinatorDetach(&link->coordinator, &p->driver);
        }
    }
    for (int i = 0; i < 2; ++i) {
        if (link->players[i].thread.impl) mCoreThreadEnd(&link->players[i].thread);
    }
    for (int i = 0; i < 2; ++i) {
        if (link->players[i].thread.impl) mCoreThreadJoin(&link->players[i].thread);
    }
    for (int i = 0; i < 2; ++i) {
        struct player *p = &link->players[i];
        if (p->thread.core) {
            mCoreConfigDeinit(&p->thread.core->config);
            p->thread.core->deinit(p->thread.core);
        }
        pthread_mutex_destroy(&p->mutex);
    }
    GBASIOLockstepCoordinatorDeinit(&link->coordinator);
    free(link);
}

void gba_link_pause(struct gba_link *link, int paused) {
    if (!link || link->paused == !!paused) return;
    for (int i = 0; i < 2; ++i) {
        struct player *p = &link->players[i];
        if (paused) mCoreThreadPause(&p->thread);
        else {
            p->deadline = now_ns();
            mCoreThreadUnpause(&p->thread);
        }
    }
    link->paused = !!paused;
}

void gba_link_keys(struct gba_link *link, int player, uint16_t keys) {
    if (link && player >= 0 && player < 2) atomic_store(&link->players[player].keys, keys & 0x3ff);
}

uint64_t gba_link_video(struct gba_link *link, int player, void *rgba) {
    if (!link || !rgba || player < 0 || player >= 2) return 0;
    struct player *p = &link->players[player];
    pthread_mutex_lock(&p->mutex);
    uint32_t *pixels = rgba;
    for (size_t i = 0; i < PIXELS; ++i) pixels[i] = p->video[i] | 0xff000000;
    uint64_t count = p->frame;
    pthread_mutex_unlock(&p->mutex);
    return count;
}

size_t gba_link_audio(struct gba_link *link, int player, int16_t *stereo, size_t frames) {
    if (!link || !stereo || player < 0 || player >= 2) return 0;
    struct player *p = &link->players[player];
    pthread_mutex_lock(&p->mutex);
    size_t count = frames < p->audio_count ? frames : p->audio_count;
    for (size_t i = 0; i < count; ++i) {
        memcpy(&stereo[i * 2], &p->audio[p->audio_read * 2], 2 * sizeof(int16_t));
        p->audio_read = (p->audio_read + 1) % AUDIO_FRAMES;
    }
    p->audio_count -= count;
    pthread_mutex_unlock(&p->mutex);
    return count;
}

unsigned gba_link_audio_rate(struct gba_link *link, int player) {
    return link && player >= 0 && player < 2 ? link->players[player].audio_rate : 0;
}

size_t gba_link_save(struct gba_link *link, int player, void *data, size_t capacity) {
    if (!link || !data || player < 0 || player >= 2) return 0;
    struct player *p = &link->players[player];
    mCoreThreadInterrupt(&p->thread);
    void *save = NULL;
    size_t size = p->thread.core->savedataClone(p->thread.core, &save);
    if (size <= capacity && save) memcpy(data, save, size);
    else size = 0;
    free(save);
    mCoreThreadContinue(&p->thread);
    return size;
}
