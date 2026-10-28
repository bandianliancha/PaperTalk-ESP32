from PIL import Image, ImageDraw, ImageFont
import os
import math

def get_font(size):
    for path in [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Unicode.ttf"
    ]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except:
                pass
    return ImageFont.load_default()

font_num = get_font(13)
font_large = get_font(26)

def draw_refined_status_bar(draw, wifi_rssi=-50, wifi_connected=True, vol_level=3, batt_pct=100, charging=False):
    # 状态栏高 32px，外框微边距
    # 1. 左侧：精致音量喇叭与圆弧声波
    sx, sy = 20, 8
    # 扬声器主体 (平滑比例)
    draw.polygon([(sx, sy + 5), (sx + 5, sy + 5), (sx + 11, sy + 1), (sx + 11, sy + 15), (sx + 5, sy + 11), (sx, sy + 11)], fill=0)
    # 声波弧线（3段平滑圆弧）
    if vol_level >= 1:
        draw.arc([sx + 9, sy + 4, sx + 16, sy + 12], -45, 45, fill=0, width=2)
    if vol_level >= 2:
        draw.arc([sx + 12, sy + 1, sx + 21, sy + 15], -45, 45, fill=0, width=2)
    if vol_level >= 3:
        draw.arc([sx + 15, sy - 2, sx + 26, sy + 18], -45, 45, fill=0, width=2)

    # 2. 中间：精致扇形/递增阶梯 WiFi 信号格
    # 采用高精度4格阶梯天线
    wx, wy = 190, 8
    if not wifi_connected:
        # WiFi未连接打叉
        draw.line([wx, wy + 2, wx + 14, wy + 14], fill=0, width=2)
        draw.line([wx + 14, wy + 2, wx, wy + 14], fill=0, width=2)
    else:
        bar_w = 3
        gap = 2
        heights = [4, 7, 11, 15]
        bars_active = 4
        if wifi_rssi < -80: bars_active = 1
        elif wifi_rssi < -70: bars_active = 2
        elif wifi_rssi < -60: bars_active = 3

        for i, h in enumerate(heights):
            bx = wx + i * (bar_w + gap)
            by = wy + (15 - h)
            if i < bars_active:
                draw.rectangle([bx, by, bx + bar_w - 1, wy + 15], fill=0)
            else:
                draw.rectangle([bx, by, bx + bar_w - 1, wy + 15], outline=0, width=1)

    # 3. 右侧：精致电量百分比 + 胶囊电池 + 闪电
    pct_text = f"{batt_pct}%"
    draw.text((312, 8), pct_text, font=font_num, fill=0)

    bx, by = 350, 7
    bw, bh = 28, 16
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=4, outline=0, width=2)
    # 正极极耳
    draw.rectangle([bx + bw + 1, by + 4, bx + bw + 3, by + bh - 4], fill=0)

    # 内部填充
    fill_len = int(22 * (max(0, min(100, batt_pct)) / 100.0))
    if fill_len > 0:
        draw.rounded_rectangle([bx + 3, by + 3, bx + 3 + fill_len, by + bh - 3], radius=2, fill=0)

    # 充电指示
    if charging:
        draw.line([bx + 15, by + 1, bx + 11, by + 8], fill=1, width=2)
        draw.line([bx + 11, by + 8, bx + 18, by + 8], fill=1, width=2)
        draw.line([bx + 18, by + 8, bx + 13, by + 15], fill=1, width=2)

    # 底部极细分割线
    draw.line([12, 30, 388, 30], fill=0, width=1)

def render_whiteboard_idle(wifi_rssi=-50, batt_pct=100, charging=False, vol=3):
    img = Image.new("1", (400, 300), 1) # 全白底板
    draw = ImageDraw.Draw(img)
    # 顶部状态栏
    draw_refined_status_bar(draw, wifi_rssi=wifi_rssi, wifi_connected=True, vol_level=vol, batt_pct=batt_pct, charging=charging)
    # 外框边线 (1px 精致装裱)
    draw.rounded_rectangle([4, 4, 396, 296], radius=10, outline=0, width=1)
    # 中间纯白！没有任何杂质
    return img

img = render_whiteboard_idle()
img.save("/tmp/test_whiteboard.png")
print("Whiteboard rendered and saved to /tmp/test_whiteboard.png")
