#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

#define CANVAS_W 400
#define CANVAS_H 300
#define CANVAS_BYTES (CANVAS_W * CANVAS_H / 8)

static inline void canvas_set_pixel(uint8_t *fb, int x, int y, uint8_t color) {
    if (x < 0 || x >= CANVAS_W || y < 0 || y >= CANVAS_H) return;
    int idx = y * (CANVAS_W / 8) + (x / 8);
    int bit = 7 - (x % 8);
    if (color == 0) {
        fb[idx] &= ~(1 << bit); // 黑色
    } else {
        fb[idx] |= (1 << bit);  // 白色
    }
}

static inline void canvas_fill_rect(uint8_t *fb, int x, int y, int w, int h, uint8_t color) {
    for (int j = 0; j < h; j++) {
        for (int i = 0; i < w; i++) {
            canvas_set_pixel(fb, x + i, y + j, color);
        }
    }
}

#include "envelope_assets.h"
#include "envelope_68.h"
#include "low_battery_asset.h"
#include "embedded_battery.h"

// 贴图函数 (带透明遮罩)
static inline void canvas_paste_masked_bitmap(uint8_t *fb, int dst_x, int dst_y,
                                              const uint8_t *bitmap, const uint8_t *mask,
                                              int w, int h) {
    int row_bytes = (w + 7) / 8;
    for (int y = 0; y < h; y++) {
        int py = dst_y + y;
        if (py < 0 || py >= CANVAS_H) continue;
        for (int x = 0; x < w; x++) {
            int px = dst_x + x;
            if (px < 0 || px >= CANVAS_W) continue;
            int b_idx = y * row_bytes + (x / 8);
            int bit = 7 - (x % 8);
            bool is_opaque = (mask[b_idx] & (1 << bit)) != 0;
            if (is_opaque) {
                bool is_white = (bitmap[b_idx] & (1 << bit)) != 0;
                canvas_set_pixel(fb, px, py, is_white ? 1 : 0);
            }
        }
    }
}

// 纯粹极简状态栏：右上角【苹果内嵌电池】
// 边界精细统一：顶部留白 = 14px，右边留白 = 14px，两边严格相等！
static inline void canvas_render_status_bar(uint8_t *fb, int batt_pct, bool charging, int vol_level, int wifi_rssi) {
    int margin = 14;
    int y_center = margin + BATT_BOX_H / 2; // 14 + 7 = 21
    int right_edge = CANVAS_W - margin;     // 400 - 14 = 386 (右侧正极边缘)
    canvas_draw_embedded_battery(fb, right_edge, y_center, batt_pct);
}

// 本地自适应信封矩阵渲染 (状态栏已消除，以整屏 400x300 为基准进行绝对居中！)
static inline void canvas_render_ui(uint8_t *fb, int unread_count, int batt_pct, bool charging, int vol_level, int wifi_rssi) {
    // 1. 全屏刷白 (1=白)
    memset(fb, 0xFF, CANVAS_BYTES);

    // 2. 状态栏直绘 (仅右上角微电池)
    canvas_render_status_bar(fb, batt_pct, charging, vol_level, wifi_rssi);

    int center_x = CANVAS_W / 2;     // 200 (水平绝对正中心)
    int net_center_y = CANVAS_H / 2; // 150 (以屏幕全高 300px 计算的垂直绝对正中心！)

    // ⭐ 终极低电量防御：电量极低且未在充电时，强制显示居中大电池充电图，阻断其余界面
    if (batt_pct <= 5 && !charging) {
        int ew = LOW_BATT_W, eh = LOW_BATT_H;
        canvas_paste_masked_bitmap(fb, center_x - ew / 2, net_center_y - eh / 2,
                                   low_battery_bitmap, low_battery_mask, ew, eh);
        return;
    }

    // 待机白板
    if (unread_count <= 0) return;

    // 1 封信 (208px)
    if (unread_count == 1) {
        int ew = ENVELOPE_208_W, eh = ENVELOPE_208_H;
        canvas_paste_masked_bitmap(fb, center_x - ew / 2, net_center_y - eh / 2,
                                   envelope_208_bitmap, envelope_208_mask, ew, eh);
    }
    // 2 封信 (146px 单排并列)
    else if (unread_count == 2) {
        int ew = ENVELOPE_146_W, eh = ENVELOPE_146_H;
        int gap = 24;
        int total_w = ew * 2 + gap;
        int start_x = center_x - total_w / 2;
        int py = net_center_y - eh / 2;
        for (int i = 0; i < 2; i++) {
            canvas_paste_masked_bitmap(fb, start_x + i * (ew + gap), py,
                                       envelope_146_bitmap, envelope_146_mask, ew, eh);
        }
    }
    // 3 封信 (106px 单排并列)
    else if (unread_count == 3) {
        int ew = ENVELOPE_106_W, eh = ENVELOPE_106_H;
        int gap = 16;
        int total_w = ew * 3 + gap * 2;
        int start_x = center_x - total_w / 2;
        int py = net_center_y - eh / 2;
        for (int i = 0; i < 3; i++) {
            canvas_paste_masked_bitmap(fb, start_x + i * (ew + gap), py,
                                       envelope_106_bitmap, envelope_106_mask, ew, eh);
        }
    }
    // 4 封信 (92px 2x2 四宫格，上下均分 2 + 2)
    else if (unread_count == 4) {
        int ew = ENVELOPE_92_W, eh = ENVELOPE_92_H;
        int gap_x = 28, gap_y = 16;
        int total_w = ew * 2 + gap_x;
        int total_h = eh * 2 + gap_y;
        int sx = center_x - total_w / 2;
        int sy = net_center_y - total_h / 2;
        for (int r = 0; r < 2; r++) {
            for (int c = 0; c < 2; c++) {
                canvas_paste_masked_bitmap(fb, sx + c * (ew + gap_x), sy + r * (eh + gap_y),
                                           envelope_92_bitmap, envelope_92_mask, ew, eh);
            }
        }
    }
    // 5~6 封信 (92px 2行均分：5封=上3下2，6封=上3下3)
    else if (unread_count <= 6) {
        int ew = ENVELOPE_92_W, eh = ENVELOPE_92_H;
        int gap_x = 18, gap_y = 16;
        int total_h = eh * 2 + gap_y;
        int sy = net_center_y - total_h / 2;

        int row1_cols = (unread_count + 1) / 2; // 5 -> 3, 6 -> 3
        int row2_cols = unread_count - row1_cols; // 5 -> 2, 6 -> 3

        // 第一行
        int row1_w = ew * row1_cols + gap_x * (row1_cols - 1);
        int sx1 = center_x - row1_w / 2;
        for (int c = 0; c < row1_cols; c++) {
            canvas_paste_masked_bitmap(fb, sx1 + c * (ew + gap_x), sy,
                                       envelope_92_bitmap, envelope_92_mask, ew, eh);
        }
        // 第二行 (居中对齐)
        int row2_w = ew * row2_cols + gap_x * (row2_cols - 1);
        int sx2 = center_x - row2_w / 2;
        for (int c = 0; c < row2_cols; c++) {
            canvas_paste_masked_bitmap(fb, sx2 + c * (ew + gap_x), sy + eh + gap_y,
                                       envelope_92_bitmap, envelope_92_mask, ew, eh);
        }
    }
    // 7~8 封信 (78px 2行均分：7封=上4下3，8封=上4下4)
    else if (unread_count <= 8) {
        int ew = ENVELOPE_78_W, eh = ENVELOPE_78_H;
        int gap_x = 16, gap_y = 14;
        int total_h = eh * 2 + gap_y;
        int sy = net_center_y - total_h / 2;

        int row1_cols = (unread_count + 1) / 2; // 7 -> 4, 8 -> 4
        int row2_cols = unread_count - row1_cols; // 7 -> 3, 8 -> 4

        // 第一行
        int row1_w = ew * row1_cols + gap_x * (row1_cols - 1);
        int sx1 = center_x - row1_w / 2;
        for (int c = 0; c < row1_cols; c++) {
            canvas_paste_masked_bitmap(fb, sx1 + c * (ew + gap_x), sy,
                                       envelope_78_bitmap, envelope_78_mask, ew, eh);
        }
        // 第二行 (自动居中)
        int row2_w = ew * row2_cols + gap_x * (row2_cols - 1);
        int sx2 = center_x - row2_w / 2;
        for (int c = 0; c < row2_cols; c++) {
            canvas_paste_masked_bitmap(fb, sx2 + c * (ew + gap_x), sy + eh + gap_y,
                                       envelope_78_bitmap, envelope_78_mask, ew, eh);
        }
    }
    // 9 封信 (⭐ 最大对称法则：3x3 完美全对称九宫格，68px，无缺角！)
    else if (unread_count == 9) {
        int ew = ENVELOPE_68_W, eh = ENVELOPE_68_H;
        int gap_x = 22, gap_y = 14;
        int total_w = ew * 3 + gap_x * 2;
        int total_h = eh * 3 + gap_y * 2;
        int sx = center_x - total_w / 2;
        int sy = net_center_y - total_h / 2;

        for (int r = 0; r < 3; r++) {
            for (int c = 0; c < 3; c++) {
                canvas_paste_masked_bitmap(fb, sx + c * (ew + gap_x), sy + r * (eh + gap_y),
                                           envelope_68_bitmap, envelope_68_mask, ew, eh);
            }
        }
    }
    // 10 封信 (⭐ 最大对称法则：2x5 完美全对称十宫格，64px)
    else if (unread_count == 10) {
        int ew = ENVELOPE_64_W, eh = ENVELOPE_64_H;
        int gap_x = 14, gap_y = 16;
        int total_w = ew * 5 + gap_x * 4;
        int total_h = eh * 2 + gap_y;
        int sx = center_x - total_w / 2;
        int sy = net_center_y - total_h / 2;

        for (int r = 0; r < 2; r++) {
            for (int c = 0; c < 5; c++) {
                canvas_paste_masked_bitmap(fb, sx + c * (ew + gap_x), sy + r * (eh + gap_y),
                                           envelope_64_bitmap, envelope_64_mask, ew, eh);
            }
        }
    }
    // 11~15 封信 (58px 3行均分矩阵：按最大对称排布，不可整除时下方少1个并居中)
    // 11: 4 + 4 + 3 (下少1)
    // 12: 4 + 4 + 4 (⭐ 3x4 完美全对称)
    // 13: 5 + 4 + 4 (上多1)
    // 14: 5 + 5 + 4 (下少1)
    // 15: 5 + 5 + 5 (⭐ 3x5 完美全对称)
    else {
        int ew = ENVELOPE_58_W, eh = ENVELOPE_58_H;
        int gap_x = 14, gap_y = 10;
        int total_h = eh * 3 + gap_y * 2;
        int sy = net_center_y - total_h / 2;

        int display_n = unread_count > 15 ? 15 : unread_count;
        int grid[3];
        if (display_n == 11) { grid[0] = 4; grid[1] = 4; grid[2] = 3; }
        else if (display_n == 12) { grid[0] = 4; grid[1] = 4; grid[2] = 4; }
        else if (display_n == 13) { grid[0] = 5; grid[1] = 4; grid[2] = 4; }
        else if (display_n == 14) { grid[0] = 5; grid[1] = 5; grid[2] = 4; }
        else { grid[0] = 5; grid[1] = 5; grid[2] = 5; } // 15

        for (int r = 0; r < 3; r++) {
            int cols_in_row = grid[r];
            int row_w = ew * cols_in_row + gap_x * (cols_in_row - 1);
            int sx = center_x - row_w / 2;
            int py = sy + r * (eh + gap_y);
            for (int c = 0; c < cols_in_row; c++) {
                canvas_paste_masked_bitmap(fb, sx + c * (ew + gap_x), py,
                                           envelope_58_bitmap, envelope_58_mask, ew, eh);
            }
        }
    }
}
