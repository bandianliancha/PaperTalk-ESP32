#include "../main/button_policy.h"
#include "../main/sound_activity.h"
#include <assert.h>
#include <stdio.h>

int main(void) {
    playback_hold_t held = { .continuous = true, .released_at_ms = -1 };
    playback_hold_update(&held, true, 0);
    playback_hold_update(&held, true, 3000);
    assert(held.continuous); // Holding throughout requests a reply after playback.

    playback_hold_t tapped = { .continuous = true, .released_at_ms = -1 };
    playback_hold_update(&tapped, false, 10);
    playback_hold_update(&tapped, false, 30);
    assert(!tapped.continuous);
    playback_hold_update(&tapped, true, 40);
    assert(!tapped.continuous); // Repeated presses cannot re-arm the reply.

    playback_hold_t bounce = { .continuous = true, .released_at_ms = -1 };
    playback_hold_update(&bounce, false, 10);
    playback_hold_update(&bounce, true, 20);
    assert(bounce.continuous);

    int16_t quiet[512] = {0};
    int16_t dc[512], sound[512];
    for (int i = 0; i < 512; i++) { dc[i] = 1200; sound[i] = (i & 1) ? 1500 : -1500; }
    assert(!audio_block_has_sound(quiet, 512));
    assert(!audio_block_has_sound(dc, 512));
    assert(audio_block_has_sound(sound, 512));
    puts("Button and sound activity tests passed");
}
