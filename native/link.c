#include "link.h"

#include <mgba/flags.h>
#include <mgba/core/core.h>
#include <mgba/core/lockstep.h>
#include <mgba/core/thread.h>
#include <mgba/core/timing.h>
#include <mgba/gba/interface.h>
#include <mgba/internal/gba/sio/lockstep.h>
#include <mgba-util/audio-buffer.h>
#include <mgba-util/audio-resampler.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <time.h>
#include <unistd.h>

#define PIXELS (240 * 160)
#define AUDIO_FRAMES 2048
#define AUDIO_RATE 48000

struct player {
    struct mAVStream stream;
    struct mCoreThread thread;
    struct mLockstepThreadUser user;
    struct GBASIOLockstepDriver driver;
    pthread_mutex_t mutex;
    atomic_uint keys;
    mColor render[PIXELS];
    mColor video[PIXELS];
    struct mAudioBuffer audio;
    struct mAudioResampler resampler;
    uint64_t frame;
    int video_fd;
    int64_t deadline;
    bool paced;
    bool attached;
};

struct gba_link {
    struct GBASIOLockstepCoordinator coordinator;
    struct player players[MAX_GBAS];
    int player_count;
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
    eventfd_write(p->video_fd, 1);
    pthread_mutex_unlock(&p->mutex);
    if (!p->paced) return;
    /* Advance the link clock so the other cores can finish their frames before we sleep. */
    struct mTimingEvent *sync = &p->driver.event;
    mTimingDeschedule(thread->core->timing, sync);
    sync->callback(thread->core->timing, sync->context, 0);
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
    unsigned rate = p->thread.core->audioSampleRate(p->thread.core);
    size_t needed = (mAudioBufferAvailable(buffer) * AUDIO_RATE + rate - 1) / rate;
    pthread_mutex_lock(&p->mutex);
    size_t available = mAudioBufferAvailable(&p->audio);
    if (available + needed > AUDIO_FRAMES) {
        mAudioBufferRead(&p->audio, NULL, available + needed - AUDIO_FRAMES);
    }
    mAudioResamplerSetSource(&p->resampler, buffer, rate, true);
    mAudioResamplerProcess(&p->resampler);
    pthread_mutex_unlock(&p->mutex);
}

struct gba_link *gba_link_create(const char *rom, const char *const *saves, int player_count) {
    if (!rom || !saves || player_count < 2 || player_count > MAX_GBAS) return NULL;
    struct gba_link *link = calloc(1, sizeof(*link));
    if (!link) return NULL;
    link->player_count = player_count;
    GBASIOLockstepCoordinatorInit(&link->coordinator);
    for (int i = 0; i < link->player_count; ++i) {
        struct player *p = &link->players[i];
        pthread_mutex_init(&p->mutex, NULL);
        atomic_init(&p->keys, 0);
        p->video_fd = -1;
        mAudioBufferInit(&p->audio, AUDIO_FRAMES, 2);
        mAudioResamplerInit(&p->resampler, mINTERPOLATOR_SINC);
        mAudioResamplerSetDestination(&p->resampler, &p->audio, AUDIO_RATE);
    }
    link->paused = true;
    for (int i = 0; i < link->player_count; ++i) {
        struct player *p = &link->players[i];
        p->paced = i == 0;
        p->video_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
        if (p->video_fd < 0) goto fail;
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
        mCoreConfigSetDefaultIntValue(&core->config, "volume", 0x100);
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
    }
    for (int i = 0; i < link->player_count; ++i) {
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
    for (int i = 0; i < link->player_count; ++i) {
        if (link->players[i].thread.impl) mCoreThreadPause(&link->players[i].thread);
    }
    for (int i = 0; i < link->player_count; ++i) {
        struct player *p = &link->players[i];
        if (p->attached) {
            p->thread.core->setPeripheral(p->thread.core, mPERIPH_GBA_LINK_PORT, NULL);
            GBASIOLockstepCoordinatorDetach(&link->coordinator, &p->driver);
        }
    }
    for (int i = 0; i < link->player_count; ++i) {
        if (link->players[i].thread.impl) mCoreThreadEnd(&link->players[i].thread);
    }
    for (int i = 0; i < link->player_count; ++i) {
        if (link->players[i].thread.impl) mCoreThreadJoin(&link->players[i].thread);
    }
    for (int i = 0; i < link->player_count; ++i) {
        struct player *p = &link->players[i];
        if (p->thread.core) {
            mCoreConfigDeinit(&p->thread.core->config);
            p->thread.core->deinit(p->thread.core);
        }
        mAudioResamplerDeinit(&p->resampler);
        mAudioBufferDeinit(&p->audio);
        pthread_mutex_destroy(&p->mutex);
        if (p->video_fd >= 0) close(p->video_fd);
    }
    GBASIOLockstepCoordinatorDeinit(&link->coordinator);
    free(link);
}

void gba_link_pause(struct gba_link *link, int paused) {
    if (!link || link->paused == !!paused) return;
    for (int i = 0; i < link->player_count; ++i) {
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
    if (link && player >= 0 && player < link->player_count) atomic_store(&link->players[player].keys, keys & 0x3ff);
}

int gba_link_video_fd(struct gba_link *link, int player) {
    return link && player >= 0 && player < link->player_count ? link->players[player].video_fd : -1;
}

uint64_t gba_link_video(struct gba_link *link, int player, void *rgba) {
    if (!link || !rgba || player < 0 || player >= link->player_count) return 0;
    struct player *p = &link->players[player];
    pthread_mutex_lock(&p->mutex);
    eventfd_t pending;
    eventfd_read(p->video_fd, &pending);
    uint32_t *pixels = rgba;
    for (size_t i = 0; i < PIXELS; ++i) pixels[i] = p->video[i] | 0xff000000;
    uint64_t count = p->frame;
    pthread_mutex_unlock(&p->mutex);
    return count;
}

size_t gba_link_audio(struct gba_link *link, int player, int16_t *stereo, size_t frames) {
    if (!link || !stereo || player < 0 || player >= link->player_count) return 0;
    struct player *p = &link->players[player];
    pthread_mutex_lock(&p->mutex);
    size_t count = mAudioBufferRead(&p->audio, stereo, frames);
    pthread_mutex_unlock(&p->mutex);
    return count;
}

unsigned gba_link_audio_rate(struct gba_link *link, int player) {
    return link && player >= 0 && player < link->player_count ? AUDIO_RATE : 0;
}

size_t gba_link_save(struct gba_link *link, int player, void *data, size_t capacity) {
    if (!link || !data || player < 0 || player >= link->player_count) return 0;
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
