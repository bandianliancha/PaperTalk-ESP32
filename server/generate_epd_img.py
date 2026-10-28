from PIL import Image, ImageDraw, ImageFont

import sys

def render_epd_image(title: str, subtitle: str, hint: str = "短按中间键收听 | 长按录音回复") -> bytes:
    # 400 x 300 1-bit EPD
    width, height = 400, 300
    img = Image.new("1", (width, height), 1) # 1 = white
    draw = ImageDraw.Draw(img)

    # 绘制外边框与卡片设计
    draw.rounded_rectangle([(10, 10), (390, 290)], radius=12, outline=0, width=2)
    
    # 顶部状态栏
    draw.text((25, 20), "💌 亲子语音留言机", fill=0)
    draw.line([(20, 48), (380, 48)], fill=0, width=1)

    # 主标题 (大字)
    draw.text((30, 80), title, fill=0)

    # 内容 (支持折行)
    # 简单按长度切分
    lines = []
    chunk_size = 14
    for i in range(0, len(subtitle), chunk_size):
        lines.append(subtitle[i:i+chunk_size])
    
    y = 130
    for l in lines[:4]:
        draw.text((30, y), l, fill=0)
        y += 28

    # 底部提示栏
    draw.line([(20, 245), (380, 245)], fill=0, width=1)
    draw.text((30, 258), hint, fill=0)

    # 转换为 1bpp raw buffer (400 * 300 / 8 = 15000 bytes)
    # PIL 1-mode: 0=black, 255/1=white
    return img.tobytes()

if __name__ == "__main__":
    b = render_epd_image("爸爸发来了一条新留言！", "你好呀儿子，今天在学校过得怎么样？记得多喝温水哦！")
    print(f"Generated EPD raw image size: {len(b)} bytes")
