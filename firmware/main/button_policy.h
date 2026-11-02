#pragma once
#include <stdbool.h>
#include <stdint.h>

// Used only when there is no playable message or a reply needs a decision.
#define BUTTON_RECORD_HOLD_MS 1200
#define BUTTON_RELEASE_DEBOUNCE_MS 20

typedef struct {
    bool continuous;
    int64_t released_at_ms;
} playback_hold_t;

// Once the initial press has been released, later presses cannot request a
// reply for this playback. A brief contact bounce is filtered.
static inline void playback_hold_update(playback_hold_t *state, bool pressed, int64_t now_ms) {
    if (!state->continuous) return;
    if (pressed) state->released_at_ms = -1;
    else if (state->released_at_ms < 0) state->released_at_ms = now_ms;
    else if (now_ms - state->released_at_ms >= BUTTON_RELEASE_DEBOUNCE_MS) state->continuous = false;
}
