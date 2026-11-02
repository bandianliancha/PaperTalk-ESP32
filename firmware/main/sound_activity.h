#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Detect actual variation rather than treating a microphone DC offset as sound.
// This deliberately only rejects near-digital silence; room noise may pass.
static inline bool audio_block_has_sound(const int16_t *samples, size_t count) {
    if (!count) return false;
    int64_t sum = 0, deviation = 0;
    for (size_t i = 0; i < count; i++) sum += samples[i];
    int32_t mean = (int32_t)(sum / (int64_t)count);
    for (size_t i = 0; i < count; i++) {
        int32_t delta = (int32_t)samples[i] - mean;
        deviation += delta < 0 ? -delta : delta;
    }
    return deviation >= (int64_t)count * 32;
}
