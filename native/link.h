#ifndef ROMM_GBA_LINK_H
#define ROMM_GBA_LINK_H

#include <stddef.h>
#include <stdint.h>

struct gba_link;

/* Control operations must be serialized; input and media reads are thread safe. */
struct gba_link *gba_link_create(const char *rom, const char *const *saves, int player_count);
void gba_link_destroy(struct gba_link *link);
void gba_link_pause(struct gba_link *link, int paused);
void gba_link_keys(struct gba_link *link, int player, uint16_t keys);
int gba_link_video_fd(struct gba_link *link, int player);
/* Reading video clears the frame notification. */
uint64_t gba_link_video(struct gba_link *link, int player, void *rgba);
size_t gba_link_audio(struct gba_link *link, int player, int16_t *stereo, size_t frames);
unsigned gba_link_audio_rate(struct gba_link *link, int player);
size_t gba_link_save(struct gba_link *link, int player, void *data, size_t capacity);

#endif
