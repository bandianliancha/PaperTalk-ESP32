from PIL import Image, ImageDraw, ImageFont
import os
from pathlib import Path

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
font_bold_num = get_font(14)
font_large = get_font(26)
font_sub = get_font(20)

def draw_status_bar(draw, wifi_rssi=-55, wifi_connected=True, vol_level=3, batt_pct=95, charging=False):
    # 顶部状态栏高度 36px，底边带一条精致细分割线
    y_center = 18

    # 1. 左侧：音量图标 (Speaker) 喇叭 + 3道精致声波弧
    sx, sy = 24, 9
    # 喇叭漏斗身
    draw.polygon([(sx, sy + 6), (sx + 5, sy + 6), (sx + 12, sy + 1), (sx + 12, sy + 17), (sx + 5, sy + 12), (sx, sy + 12)], fill=0)
    # 声波
    if vol_level >= 1:
        draw.arc([sx + 10, sy + 5, sx + 17, sy + 13], -50, 50, fill=0, width=2)
    if vol_level >= 2:
        draw.arc([sx + 13, sy + 2, sx + 22, sy + 16], -50, 50, fill=0, width=2)
    if vol_level >= 3:
        draw.arc([sx + 16, sy - 1, sx + 27, sy + 19], -50, 50, fill=0, width=2)

    # 2. 中间：WiFi 信号格 (4根高度递增竖条)
    wx, wy = 190, 10
    if not wifi_connected:
        # WiFi 叉号
        draw.line([wx, wy, wx + 16, wy + 16], fill=0, width=2)
        draw.line([wx + 16, wy, wx, wy + 16], fill=0, width=2)
    else:
        # 4条渐进竖线
        bar_w = 3
        gap = 2
        heights = [4, 8, 12, 16]
        # 判断格数
        bars_active = 4
        if wifi_rssi < -80: bars_active = 1
        elif wifi_rssi < -70: bars_active = 2
        elif wifi_rssi < -60: bars_active = 3

        for i, h in enumerate(heights):
            bx = wx + i * (bar_w + gap)
            by = wy + (16 - h)
            if i < bars_active:
                draw.rectangle([bx, by, bx + bar_w - 1, wy + 16], fill=0)
            else:
                draw.rectangle([bx, by, bx + bar_w - 1, wy + 16], outline=0, width=1)

    # 3. 右侧：精致电池外壳 + 精确电量百分比数字 + 充电指示
    # 电量数字显示 (如 "95%")
    pct_text = f"{batt_pct}%"
    draw.text((310, 10), pct_text, font=font_num, fill=0)

    bx, by = 345, 9
    bw, bh = 30, 16
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=3, outline=0, width=2)
    # 电池正极小凸起
    draw.rectangle([bx + bw + 1, by + 4, bx + bw + 3, by + bh - 4], fill=0)

    # 电池内部填充条 (内部有效宽度 24)
    fill_len = int(24 * (max(0, min(100, batt_pct)) / 100.0))
    if fill_len > 0:
        draw.rectangle([bx + 3, by + 3, bx + 3 + fill_len, by + bh - 3], fill=0)

    # 如果正在充电，在电池中央叠加上精致闪电 ⚡
    if charging:
        draw.line([bx + 16, by + 1, bx + 12, by + 8], fill=1, width=2)
        draw.line([bx + 12, by + 8, bx + 19, by + 8], fill=1, width=2)
        draw.line([bx + 19, by + 8, bx + 14, by + 15], fill=1, width=2)

    # 状态栏底部细线
    draw.line([12, 32, 388, 32], fill=0, width=1)

def render_state(state="idle", wifi_rssi=-55, batt_pct=98, charging=False, vol=3):
    img = Image.new("1", (400, 300), 1) # 纯白底板
    draw = ImageDraw.Draw(img)

    # 顶部精致状态栏
    draw_status_bar(draw, wifi_rssi=wifi_rssi, wifi_connected=True, vol_level=vol, batt_pct=batt_pct, charging=charging)

    # 边缘精致微圆角轮廓 (给整个设备屏增加卡片装裱感)
    draw.rounded_rectangle([4, 4, 396, 296], radius=10, outline=0, width=1)

    if state == "idle":
        # 极简纯净白板！中间没有任何杂质，完全留白
        pass

    elif state == "new_message":
        # 💌 爸爸来留言了：大信封 + 爱心 + 底部单按指引
        cx, cy = 200, 125
        w, h = 110, 72
        x0, y0 = cx - w // 2, cy - h // 2
        x1, y1 = x0 + w, y0 + h
        draw.rounded_rectangle([x0, y0, x1, y1], radius=8, outline=0, width=3)
        draw.line([x0, y0, cx, cy + 10], fill=0, width=3)
        draw.line([x1, y0, cx, cy + 10], fill=0, width=3)
        # 爸爸的心意爱心
        draw.arc([186, 50, 202, 68], 180, 360, fill=0, width=3)
        draw.arc([198, 50, 214, 68], 180, 360, fill=0, width=3)
        draw.line([186, 59, 200, 76], fill=0, width=3)
        draw.line([214, 59, 200, 76], fill=0, width=3)

        # 底部按键符号指引
        draw.ellipse([140, 215, 168, 243], outline=0, width=3)
        draw.ellipse([148, 223, 160, 235], fill=0)
        draw.text((180, 218), "按一下听留言", font=font_large, fill=0)

    elif state == "recording":
        # 🎙️ 录音中：立式麦克风 + 扩散声波 + 底部提示
        cx, cy = 200, 125
        draw.rounded_rectangle([cx - 15, cy - 35, cx + 15, cy + 10], radius=15, fill=0)
        draw.arc([cx - 26, cy - 14, cx + 26, cy + 22], 0, 180, fill=0, width=4)
        draw.line([cx, cy + 22, cx, cy + 42], fill=0, width=4)
        draw.line([cx - 20, cy + 42, cx + 20, cy + 42], fill=0, width=4)
        # 声波
        draw.arc([cx - 50, cy - 30, cx + 50, cy + 30], -60, 60, fill=0, width=3)
        draw.arc([cx - 65, cy - 45, cx + 65, cy + 45], -60, 60, fill=0, width=3)

        draw.text((130, 220), "松开就发给爸爸", font=font_large, fill=0)

    elif state == "sending":
        # 🚀 正在发送给爸爸：小火箭升空
        cx, cy = 200, 125
        # 火箭体
        draw.polygon([(cx, cy - 45), (cx + 20, cy + 10), (cx - 20, cy + 10)], fill=0)
        draw.ellipse([cx - 6, cy - 15, cx + 6, cy - 3], fill=1) # 小舷窗
        # 火箭翼
        draw.polygon([(cx - 20, cy), (cx - 32, cy + 20), (cx - 20, cy + 15)], fill=0)
        draw.polygon([(cx + 20, cy), (cx + 32, cy + 20), (cx + 20, cy + 15)], fill=0)
        # 尾焰
        draw.line([cx - 10, cy + 16, cx - 10, cy + 30], fill=0, width=2)
        draw.line([cx, cy + 16, cx, cy + 38], fill=0, width=3)
        draw.line([cx + 10, cy + 16, cx + 10, cy + 30], fill=0, width=2)

        draw.text((140, 220), "正在发给爸爸...", font=font_large, fill=0)

    return img

if __name__ == "__main__":
    img = render_state("idle")
    img.save("/tmp/preview_idle.png")
    img2 = render_state("new_message")
    img2.save("/tmp/preview_msg.png")
    print("UI previews generated successfully!")
