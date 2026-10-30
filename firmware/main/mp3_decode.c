#define MINIMP3_ONLY_MP3
#define MINIMP3_IMPLEMENTATION
#include "vendor/minimp3/minimp3.h"
#include <stdlib.h>

void *voice_mp3_create(void) {
    mp3dec_t *decoder = calloc(1, sizeof(*decoder));
    if (decoder) mp3dec_init(decoder);
    return decoder;
}
int voice_mp3_frame(void *decoder, const unsigned char *frame, short *samples) {
    mp3dec_frame_info_t info = {0};
    int count = mp3dec_decode_frame(decoder, frame, 144, samples, &info);
    if (info.frame_bytes != 144 || info.hz != 16000 || info.channels != 1 || info.layer != 3 || info.bitrate_kbps != 32)
        return -1;
    return count;
}
