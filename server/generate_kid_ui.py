from PIL import Image, ImageDraw, ImageFont
import os
from pathlib import Path

def get_font(size):
    for path in [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf"
    ]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except:
                pass
    return ImageFont.load_default()

def draw_battery(draw, x, y, pct=85, charging=False):
    # 电池外壳 34x18
    draw.rounded_rectangle([x, y, x + 34, y + 18], radius=3, outline=0, width=2)
    draw.rectangle([x + 35, y + 5, x + 38, y + 13], fill=0) # 电池正极触头
    # 电量填充
    inner_w = int(28 * (max(5, min(100, pct)) / 100.0))
    draw.rectangle([x + 3, y + 3, x + 3 + inner_w, y + 15], fill=0)
    
    # 闪电充电图标
    if charging:
        draw.line([x + 18, y + 2, x + 13, y + 9], fill=1, width=2)
        draw.line([x + 13, y + 9, x + 20, y + 9], fill=1, width=2)
        draw.line([x + 20, y + 9, x + 15, y + 16], fill=1, width=2)

def draw_speaker(draw, x, y, volume_level=3):
    # 小喇叭身体
    draw.polygon([(x + 2, y + 6), (x + 8, y + 6), (x + 16, y + 1), (x + 16, y + 19), (x + 8, y + 14), (x + 2, y + 14)], fill=0)
    # 声波弧线
    if volume_level >= 1:
        draw.arc([x + 14, y + 6, x + 20, y + 14], -60, 60, fill=0, width=2)
    if volume_level >= 2:
        draw.arc([x + 16, y + 3, x + 25, y + 17], -60, 60, fill=0, width=2)
    if volume_level >= 3:
        draw.arc([x + 18, y + 0, x + 30, y + 20], -60, 60, fill=0, width=2)

def draw_envelope(draw, cx, cy, size=60):
    # 信封图标 120x80
    w, h = size * 2, int(size * 1.3)
    x0, y0 = cx - w // 2, cy - h // 2
    x1, y1 = x0 + w, y0 + h
    draw.rounded_rectangle([x0, y0, x1, y1], radius=8, outline=0, width=4)
    # 折角线条
    draw.line([x0, y0, cx, cy + 10], fill=0, width=4)
    draw.line([x1, y0, cx, cy + 10], fill=0, width=4)
    # 底部折线
    draw.line([x0, y1, cx - 20, cy + 8], fill=0, width=2)
    draw.line([x1, y1, cx + 20, cy + 8], fill=0, width=2)

def draw_kid_face(draw, cx, cy, mood="happy"):
    # 圆脸
    r = 55
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=4)
    # 眼睛
    draw.ellipse([cx - 24, cy - 20, cx - 14, cy - 6], fill=0)
    draw.ellipse([cx + 14, cy - 20, cx + 24, cy - 6], fill=0)
    # 嘴巴
    if mood == "happy":
        draw.arc([cx - 28, cy - 10, cx + 28, cy + 30], 20, 160, fill=0, width=4)
    elif mood == "speaking":
        draw.ellipse([cx - 15, cy + 8, cx + 15, cy + 32], fill=0)

def draw_mic(draw, cx, cy):
    # 麦克风图标
    draw.rounded_rectangle([cx - 15, cy - 35, cx + 15, cy + 10], radius=15, fill=0)
    # 托架
    draw.arc([cx - 26, cy - 15, cx + 26, cy + 22], 0, 180, fill=0, width=4)
    draw.line([cx, cy + 22, cx, cy + 42], fill=0, width=4)
    draw.line([cx - 22, cy + 42, cx + 22, cy + 42], fill=0, width=4)

def render_child_screen(state: str = "new_message", battery_pct=85, volume_level=3, charging=False):
    # 400x300 纯白底图
    img = Image.new("1", (400, 300), 1)
    draw = ImageDraw.Draw(img)

    # 1. 顶部状态栏 (高度 40px)
    draw_speaker(draw, 25, 12, volume_level)
    draw_battery(draw, 340, 12, battery_pct, charging)
    draw.line([15, 38, 385, 38], fill=0, width=2)

    # 2. 中间大图标与图形交互区 (孩子专属易懂模式)
    font_large = get_font(28)
    font_sub = get_font(22)

    if state == "new_message":
        # 爸爸发来留言：大大信封 + 心形 💌
        draw_envelope(draw, 200, 120, size=55)
        # 爸爸爱心
        draw.arc([185, 48, 203, 66], 180, 360, fill=0, width=3)
        draw.arc([197, 48, 215, 66], 180, 360, fill=0, width=3)
        draw.line([185, 57, 200, 75], fill=0, width=3)
        draw.line([215, 57, 200, 75], fill=0, width=3)
        # 底部按键指引图形：按键图标 🔘
        draw.ellipse([145, 210, 175, 240], outline=0, width=3)
        draw.ellipse([153, 218, 167, 232], fill=0)
        draw.text((188, 213), "按一下听留言", font=font_large, fill=0)

    elif state == "recording":
        # 录音中：大麦克风 🎙️ + 声波动态
        draw_mic(draw, 200, 120)
        # 声波扩散线
        draw.arc([130, 85, 270, 155], -60, 60, fill=0, width=3)
        draw.arc([115, 70, 285, 170], -60, 60, fill=0, width=3)
        # 底部松开指引
        draw.text((135, 215), "松开就发给爸爸", font=font_large, fill=0)

    elif state == "idle":
        # 平时待机：笑脸 😊 + 提示长按说话
        draw_kid_face(draw, 200, 115, mood="happy")
        draw.ellipse([125, 210, 155, 240], outline=0, width=3)
        draw.ellipse([133, 218, 147, 232], fill=0)
        draw.text((168, 213), "按住说话给爸爸", font=font_large, fill=0)

    # 边框装饰
    draw.rounded_rectangle([6, 6, 394, 294], radius=14, outline=0, width=2)

    return img.tobytes()

if __name__ == "__main__":
    raw = render_child_screen("new_message")
    print(f"Child UI rendered: {len(raw)} bytes")
