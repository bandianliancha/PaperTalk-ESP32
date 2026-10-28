#pragma once
#include <stdint.h>
#include <string.h>
#include <stdio.h>

#define BATT_BOX_W 28
#define BATT_BOX_H 14

typedef struct {
    char c;
    uint8_t w;
    uint16_t rows[14];
} batt_num_glyph_t;

static const batt_num_glyph_t batt_num_glyphs[] = {
    { '0', 5, {
        0x0000, 0x0000, 0x0000, 0x7000, 0x8800, 0x8800, 0x8800, 0x8800, 0x8800, 0x7000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '1', 3, {
        0x0000, 0x0000, 0x0000, 0x2000, 0xe000, 0x2000, 0x2000, 0x2000, 0x2000, 0x2000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '2', 5, {
        0x0000, 0x0000, 0x0000, 0x7000, 0x8800, 0x0800, 0x3000, 0x4000, 0x8000, 0xf800, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '3', 5, {
        0x0000, 0x0000, 0x0000, 0x7000, 0x8800, 0x0800, 0x3000, 0x0800, 0x8800, 0x7000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '4', 5, {
        0x0000, 0x0000, 0x0000, 0x1000, 0x3000, 0x5000, 0x9000, 0xf800, 0x1000, 0x1000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '5', 4, {
        0x0000, 0x0000, 0x0000, 0xf000, 0x0000, 0xe000, 0x1000, 0x1000, 0x1000, 0xe000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '6', 5, {
        0x0000, 0x0000, 0x0000, 0x7000, 0x8800, 0xf000, 0x8800, 0x8800, 0x8800, 0x7000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '7', 5, {
        0x0000, 0x0000, 0x0000, 0xf800, 0x0800, 0x1000, 0x2000, 0x2000, 0x4000, 0x4000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '8', 5, {
        0x0000, 0x0000, 0x0000, 0x7000, 0x8800, 0x8800, 0x7000, 0x8800, 0x8800, 0x7000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
    { '9', 5, {
        0x0000, 0x0000, 0x0000, 0x7000, 0x8800, 0x8800, 0x8800, 0x7800, 0x8800, 0x7000, 0x0000, 0x0000, 0x0000, 0x0000
     } },
};


static inline void canvas_draw_embedded_battery(uint8_t *fb, int right_x, int y_center, int pct) {
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;

    int bw = BATT_BOX_W;
    int bh = BATT_BOX_H;
    int bx = right_x - bw - 2;
    int by = y_center - bh / 2; // Y=9

    // 1. 绘制电池外框
    for (int x = bx + 2; x <= bx + bw - 2; x++) {
        canvas_set_pixel(fb, x, by, 0);
        canvas_set_pixel(fb, x, by + bh - 1, 0);
    }
    for (int y = by + 2; y <= by + bh - 3; y++) {
        canvas_set_pixel(fb, bx, y, 0);
        canvas_set_pixel(fb, bx + bw - 1, y, 0);
    }
    canvas_set_pixel(fb, bx + 1, by + 1, 0);
    canvas_set_pixel(fb, bx + bw - 2, by + 1, 0);
    canvas_set_pixel(fb, bx + 1, by + bh - 2, 0);
    canvas_set_pixel(fb, bx + bw - 2, by + bh - 2, 0);

    // 2. 正极微凸起
    for (int y = y_center - 2; y <= y_center + 2; y++) {
        canvas_set_pixel(fb, bx + bw, y, 0);
        canvas_set_pixel(fb, bx + bw + 1, y, 0);
    }

    // 3. 动态液位充填
    int max_fill_w = bw - 4; // 24px
    int fill_w = (max_fill_w * pct) / 100;
    for (int j = 2; j < bh - 2; j++) {
        for (int i = 0; i < fill_w; i++) {
            canvas_set_pixel(fb, bx + 2 + i, by + j, 0);
        }
    }

    // 4. 计算数字宽度并居中
    char num_str[8];
    snprintf(num_str, sizeof(num_str), "%d", pct);
    int total_num_w = 0;
    for (int i = 0; num_str[i]; i++) {
        for (int g = 0; g < 10; g++) {
            if (batt_num_glyphs[g].c == num_str[i]) {
                total_num_w += batt_num_glyphs[g].w + 1;
                break;
            }
        }
    }
    if (total_num_w > 0) total_num_w--;

    int start_num_x = bx + (bw - total_num_w) / 2;

    // 5. 异或反色打点
    int cur_x = start_num_x;
    for (int s = 0; num_str[s]; s++) {
        char ch = num_str[s];
        for (int g = 0; g < 10; g++) {
            if (batt_num_glyphs[g].c == ch) {
                int gw = batt_num_glyphs[g].w;
                for (int y = 0; y < bh; y++) {
                    uint16_t row = batt_num_glyphs[g].rows[y];
                    if (!row) continue;
                    for (int x = 0; x < gw; x++) {
                        if (row & (1U << (15 - x))) {
                            int px = cur_x + x;
                            int py = by + y;
                            int idx = py * (CANVAS_W / 8) + (px / 8);
                            int bit = 7 - (px % 8);
                            fb[idx] ^= (1 << bit);
                        }
                    }
                }
                cur_x += gw + 1;
                break;
            }
        }
    }
}
