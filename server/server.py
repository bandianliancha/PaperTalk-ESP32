import os
import sys
import time
import json
import math
import asyncio
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from aiohttp import web
import aiohttp
import requests
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont
import lark_oapi as lark

BASE_DIR = Path(__file__).resolve().parent

# ── 加载 .env 配置文件 ───────────────────────────────────────
env_path = BASE_DIR / ".env"
if env_path.exists():
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DEFAULT_OPEN_ID = os.environ.get("DEFAULT_OPEN_ID", "")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("FeishuBridge")

device_ws_clients = set()
current_open_id = DEFAULT_OPEN_ID
latest_epd_img_path = DATA_DIR / "screen.bin"

# 未读消息队列：FIFO 结构
unread_messages = []

# 设备状态
device_state = {
    "battery_pct": 100,
    "charging": False,
    "volume_level": 2, # 1:小, 2:中, 3:大
    "wifi_rssi": -55,
    "wifi_connected": True,
    "ui_state": "idle" # "idle", "new_message", "recording", "sending"
}

lark_client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .log_level(lark.LogLevel.INFO) \
    .build()

executor = ThreadPoolExecutor(max_workers=4)
server_loop = None

def get_font(size):
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFCompact.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/Library/Fonts/Arial.ttf"
    ]:
        try:
            return ImageFont.truetype(path, size)
        except:
            pass
    return ImageFont.load_default()

font_time = get_font(13)
font_pct = get_font(12)
font_num = get_font(10)

try:
    font_emoji = ImageFont.truetype("/System/Library/Fonts/Apple Color Emoji.ttc", 160)
except Exception:
    font_emoji = None

SCREEN_W = 400
SCREEN_H = 300
NET_CENTER_Y = SCREEN_H // 2  # 150 (以全屏整体高度为基准的垂直正中心)
CENTER_X = SCREEN_W // 2      # 200 (水平正中心)

def get_cropped_emoji(emoji_char):
    raw_box = 240
    im = Image.new("RGBA", (raw_box, raw_box), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((raw_box // 2, raw_box // 2), emoji_char, font=font_emoji, embedded_color=True, anchor="mm")
    bbox = im.getbbox()
    if not bbox:
        return im
    return im.crop(bbox)

cropped_heart = get_cropped_emoji("💌") if font_emoji else None
w_orig, h_orig = cropped_heart.size if cropped_heart else (158, 131)
aspect_heart = h_orig / w_orig

def get_sized_emoji_1bit(emoji_cropped, target_w, aspect):
    target_h = int(target_w * aspect)
    resized = emoji_cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)
    dithered = resized.convert("L").convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    return dithered, resized.split()[3], target_w, target_h

def draw_apple_wifi(draw, center_x, center_y, wifi_connected=True, wifi_rssi=-55):
    """苹果经典扇形弧线 Wi-Fi 图标 (严格高度对齐基准)"""
    if not wifi_connected:
        draw.line([center_x - 6, center_y - 6, center_x + 6, center_y + 6], fill=0, width=1)
        draw.line([center_x + 6, center_y - 6, center_x - 6, center_y + 6], fill=0, width=1)
        return

    # 底部中心发射点
    draw.ellipse([center_x - 1, center_y + 4, center_x + 1, center_y + 6], fill=0)
    # 第一圈内弧 (高度覆盖 -1 到 +9)
    if wifi_rssi >= -75:
        draw.arc([center_x - 5, center_y - 1, center_x + 5, center_y + 9], 220, 320, fill=0, width=1)
    # 第二圈外弧 (高度覆盖 -6 到 +12，整体高度恰好为 12px，与电池高度 100% 齐平)
    if wifi_rssi >= -65:
        draw.arc([center_x - 9, center_y - 6, center_x + 9, center_y + 12], 225, 315, fill=0, width=1)

def draw_sharp_status_bar(draw, w=400, batt_pct=100, charging=False, wifi_rssi=-55, wifi_connected=True, vol_level=2):
    """
    精密视觉对齐状态栏 (Pixel-Perfect Alignment):
    状态栏总高 32px，绝对几何中轴线位于 Y = 16.0
    时间、音量喇叭、Wi-Fi 弧面、电量百分比、电池图标全部统一在 Y=10 ~ Y=22 (高度 12px) 严格齐平！
    """
    y_center = 16

    # 1. 纯净极简设计：去掉时间显示（无需唤醒对时，极度省电且画面清爽）

    # 2. 苹果经典内嵌数字电池：边距绝对统一（顶部 14px，右边 14px）
    margin = 14
    bw, bh = 28, 14
    right_x = w - margin
    bx = right_x - bw - 2  # 400 - 14 - 28 - 2 = 356
    by = margin            # 14 (距离顶部严格 14px)
    y_center = by + bh // 2  # 21

    # 电池外壳与正极凸起
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=2, outline=0, width=1)
    draw.rectangle([bx + bw + 1, y_center - 3, bx + bw + 2, y_center + 3], fill=0)

    # 动态液位充填
    fill_w = int((bw - 4) * (max(0, min(100, batt_pct)) / 100.0))
    if fill_w > 0:
        draw.rectangle([bx + 2, by + 2, bx + 2 + fill_w, by + bh - 2], fill=0)

    # 数字智能反色居中 (XOR)
    num_str = str(batt_pct)
    im_num = Image.new("1", (bw, bh), 0)
    d_num = ImageDraw.Draw(im_num)
    d_num.text((bw // 2, bh // 2), num_str, font=font_num, fill=1, anchor="mm")
    for y in range(bh):
        for x in range(bw):
            if im_num.getpixel((x, y)) == 1:
                orig = draw._image.getpixel((bx + x, by + y))
                draw._image.putpixel((bx + x, by + y), 1 if orig == 0 else 0)

def render_child_screen(state: str = None, battery_pct=None, volume_level=None, charging=None, wifi_rssi=None) -> bytes:
    """
    全量支持 1 到 15 封信满屏动态矩阵排布体系：
    1 封: 1 个大信封居中 (208px)
    2 封: 2 个信封并排 (146px)
    3 封: 3 个信封并排 (106px)
    4 封: 2x2 四宫格 (110px)
    5~6 封: 2x3 布局 (92px)
    7~8 封: 2x4 布局 (78px)
    9~10 封: 2x5 布局 (62px)
    11~15 封: 3x5 布局 (58px，最多完整呈现 15 封信！随着听完逐一消减)
    """
    if state is not None:
        device_state["ui_state"] = state
    if battery_pct is not None:
        device_state["battery_pct"] = battery_pct
    if volume_level is not None:
        device_state["volume_level"] = volume_level
    if charging is not None:
        device_state["charging"] = charging
    if wifi_rssi is not None:
        device_state["wifi_rssi"] = wifi_rssi

    cur_state = device_state["ui_state"]
    pct = device_state["battery_pct"]
    chg = device_state["charging"]
    vol = device_state["volume_level"]
    rssi = device_state["wifi_rssi"]

    im_final = Image.new("1", (SCREEN_W, SCREEN_H), 1)
    unread_count = len(unread_messages)

    if cur_state == "new_message" and cropped_heart:
        if unread_count <= 1:
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 208, aspect_heart)
            px = CENTER_X - ew // 2
            py = NET_CENTER_Y - eh // 2
            im_final.paste(img_1b, (px, py), mask)

        elif unread_count == 2:
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 146, aspect_heart)
            gap = 24
            total_w = ew * 2 + gap
            start_x = CENTER_X - total_w // 2
            py = NET_CENTER_Y - eh // 2
            for i in range(2):
                im_final.paste(img_1b, (start_x + i * (ew + gap), py), mask)

        elif unread_count == 3:
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 106, aspect_heart)
            gap = 16
            total_w = ew * 3 + gap * 2
            start_x = CENTER_X - total_w // 2
            py = NET_CENTER_Y - eh // 2
            for i in range(3):
                im_final.paste(img_1b, (start_x + i * (ew + gap), py), mask)

        elif unread_count == 4:
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 110, aspect_heart)
            gap_x, gap_y = 32, 16
            total_w = ew * 2 + gap_x
            total_h = eh * 2 + gap_y
            start_x = CENTER_X - total_w // 2
            start_y = NET_CENTER_Y - total_h // 2
            for row in range(2):
                for col in range(2):
                    im_final.paste(img_1b, (start_x + col * (ew + gap_x), start_y + row * (eh + gap_y)), mask)

        elif unread_count in (5, 6):
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 92, aspect_heart)
            cols = 3
            gap_x, gap_y = 20, 16
            total_w = ew * cols + gap_x * (cols - 1)
            total_h = eh * 2 + gap_y
            start_x = CENTER_X - total_w // 2
            start_y = NET_CENTER_Y - total_h // 2

            coords = []
            if unread_count == 5:
                for col in range(3):
                    coords.append((start_x + col * (ew + gap_x), start_y))
                row2_w = ew * 2 + gap_x
                row2_start_x = CENTER_X - row2_w // 2
                for col in range(2):
                    coords.append((row2_start_x + col * (ew + gap_x), start_y + eh + gap_y))
            else:
                for row in range(2):
                    for col in range(3):
                        coords.append((start_x + col * (ew + gap_x), start_y + row * (eh + gap_y)))

            for (px, py) in coords:
                im_final.paste(img_1b, (px, py), mask)

        elif unread_count in (7, 8):
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 78, aspect_heart)
            cols = 4
            gap_x, gap_y = 16, 16
            total_w = ew * cols + gap_x * (cols - 1)
            total_h = eh * 2 + gap_y
            start_x = CENTER_X - total_w // 2
            start_y = NET_CENTER_Y - total_h // 2

            coords = []
            if unread_count == 7:
                for col in range(4):
                    coords.append((start_x + col * (ew + gap_x), start_y))
                row2_w = ew * 3 + gap_x * 2
                row2_start_x = CENTER_X - row2_w // 2
                for col in range(3):
                    coords.append((row2_start_x + col * (ew + gap_x), start_y + eh + gap_y))
            else:
                for row in range(2):
                    for col in range(4):
                        coords.append((start_x + col * (ew + gap_x), start_y + row * (eh + gap_y)))

            for (px, py) in coords:
                im_final.paste(img_1b, (px, py), mask)

        elif unread_count == 9:
            # ⭐ 最大对称法则：3x3 完美全对称九宫格，68px，无缺角！
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 68, aspect_heart)
            gap_x, gap_y = 22, 14
            total_w = ew * 3 + gap_x * 2
            total_h = eh * 3 + gap_y * 2
            start_x = CENTER_X - total_w // 2
            start_y = NET_CENTER_Y - total_h // 2

            coords = []
            for r in range(3):
                for c in range(3):
                    coords.append((start_x + c * (ew + gap_x), start_y + r * (eh + gap_y)))

            for (px, py) in coords:
                im_final.paste(img_1b, (px, py), mask)

        elif unread_count == 10:
            # ⭐ 最大对称法则：2x5 完美全对称十宫格，64px！
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 64, aspect_heart)
            gap_x, gap_y = 14, 16
            total_w = ew * 5 + gap_x * 4
            total_h = eh * 2 + gap_y
            start_x = CENTER_X - total_w // 2
            start_y = NET_CENTER_Y - total_h // 2

            coords = []
            for r in range(2):
                for c in range(5):
                    coords.append((start_x + c * (ew + gap_x), start_y + r * (eh + gap_y)))

            for (px, py) in coords:
                im_final.paste(img_1b, (px, py), mask)

        else:
            # 11 ~ 15 封：3 行均分矩阵（保证行间平衡，不够时下面少1个，左右居中）
            img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_heart, 58, aspect_heart)
            display_n = min(15, unread_count)
            rows = 3
            base = display_n // rows
            rem = display_n % rows
            grid = [base + (1 if r < rem else 0) for r in range(rows)]
            
            gap_x, gap_y = 14, 10
            total_h = eh * rows + gap_y * (rows - 1)
            start_y = NET_CENTER_Y - total_h // 2

            coords = []
            for r, cols_in_row in enumerate(grid):
                row_w = ew * cols_in_row + gap_x * (cols_in_row - 1)
                row_start_x = CENTER_X - row_w // 2
                py = start_y + r * (eh + gap_y)
                for c in range(cols_in_row):
                    coords.append((row_start_x + c * (ew + gap_x), py))

            for (px, py) in coords:
                im_final.paste(img_1b, (px, py), mask)

    elif cur_state == "recording":
        cropped_mic = get_cropped_emoji("🎙️")
        w_m, h_m = cropped_mic.size
        img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_mic, 160, h_m / w_m)
        px = CENTER_X - ew // 2
        py = NET_CENTER_Y - eh // 2
        im_final.paste(img_1b, (px, py), mask)

    elif cur_state == "sending":
        cropped_rocket = get_cropped_emoji("🚀")
        w_r, h_r = cropped_rocket.size
        img_1b, mask, ew, eh = get_sized_emoji_1bit(cropped_rocket, 170, h_r / w_r)
        px = CENTER_X - ew // 2
        py = NET_CENTER_Y - eh // 2
        im_final.paste(img_1b, (px, py), mask)

    # 顶部状态栏直绘
    d_final = ImageDraw.Draw(im_final)
    draw_sharp_status_bar(d_final, w=SCREEN_W, batt_pct=pct, charging=chg, wifi_rssi=rssi, wifi_connected=True, vol_level=vol)

    raw = im_final.tobytes()
    with open(latest_epd_img_path, "wb") as f:
        f.write(raw)
    return raw

# 初始界面
render_child_screen("idle", battery_pct=100, volume_level=2, wifi_rssi=-55)

def sync_generate_tts(text_to_speak: str, out_wav: Path):
    import edge_tts
    tmp_mp3 = DATA_DIR / f"tts_{int(time.time() * 1000)}.mp3"
    async def _save():
        communicate = edge_tts.Communicate(text_to_speak, voice="zh-CN-YunxiNeural")
        await communicate.save(str(tmp_mp3))
    
    new_loop = asyncio.new_event_loop()
    try:
        new_loop.run_until_complete(_save())
    finally:
        new_loop.close()

    convert_to_wav(tmp_mp3, out_wav)
    if tmp_mp3.exists():
        tmp_mp3.unlink()

def get_tenant_access_token():
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        res = requests.post(url, json={
            "app_id": APP_ID,
            "app_secret": APP_SECRET
        }, timeout=5).json()
        return res.get("tenant_access_token")
    except Exception as e:
        logger.error(f"Failed to get token: {e}")
        return None

def clear_and_set_message_reaction(message_id: str, new_emoji_type: str):
    token = get_tenant_access_token()
    if not token or not message_id:
        return
    headers = {"Authorization": f"Bearer {token}"}

    try:
        r_list = requests.get(f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions", headers=headers, timeout=5).json()
        items = r_list.get("data", {}).get("items", [])
        for item in items:
            if item.get("operator", {}).get("operator_id") == APP_ID:
                r_id = item.get("reaction_id")
                requests.delete(f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions/{r_id}", headers=headers, timeout=5)
    except Exception as e:
        logger.warning(f"Failed to clean old reactions: {e}")

    try:
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions"
        body = {"reaction_type": {"emoji_type": new_emoji_type}}
        r = requests.post(url, headers=headers, json=body, timeout=5)
        logger.info(f"Replaced reaction on msg {message_id} -> '{new_emoji_type}': {r.json().get('msg')}")
    except Exception as e:
        logger.error(f"Failed to add new reaction: {e}")

async def notify_device(payload: dict):
    if not device_ws_clients:
        logger.warning("No ESP32 device currently connected via WebSocket.")
        return False
    msg_str = json.dumps(payload)
    for ws in list(device_ws_clients):
        try:
            await ws.send_str(msg_str)
        except Exception as e:
            logger.error(f"Failed to send to device: {e}")
    return True

def download_feishu_resource(message_id: str, file_key: str, dest_path: Path):
    token = get_tenant_access_token()
    if not token:
        return False
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, stream=True)
    if r.status_code == 200:
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    else:
        logger.error(f"Failed to download resource: {r.status_code} {r.text}")
        return False

def convert_to_wav(src_path: Path, dst_wav_path: Path):
    cmd = [
        "ffmpeg", "-y", "-i", str(src_path),
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        str(dst_wav_path)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def convert_to_opus(src_wav: Path, dst_opus: Path):
    cmd = [
        "ffmpeg", "-y", "-i", str(src_wav),
        "-c:a", "libopus", "-b:a", "16k", "-ar", "16000", "-ac", "1",
        str(dst_opus)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def upload_and_send_voice_to_feishu(wav_path: Path, open_id: str):
    opus_path = wav_path.with_suffix(".opus")
    convert_to_opus(wav_path, opus_path)
    
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(wav_path)]
    try:
        dur_s = float(subprocess.check_output(cmd).decode().strip())
        dur_ms = int(dur_s * 1000)
    except:
        dur_ms = 3000

    token = get_tenant_access_token()
    upload_url = "https://open.feishu.cn/open-apis/im/v1/files"
    headers = {"Authorization": f"Bearer {token}"}
    with open(opus_path, "rb") as f:
        files = {
            "file_type": (None, "opus"),
            "file_name": (None, "voice.opus"),
            "duration": (None, str(dur_ms)),
            "file": ("voice.opus", f, "audio/opus")
        }
        r = requests.post(upload_url, headers=headers, files=files)
        res = r.json()
        if res.get("code") != 0:
            logger.error(f"Feishu file upload failed: {res}")
            return False
        file_key = res["data"]["file_key"]
        logger.info(f"Uploaded audio to Feishu file_key: {file_key}")

    send_url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    body = {
        "receive_id": open_id,
        "msg_type": "audio",
        "content": json.dumps({"file_key": file_key})
    }
    r = requests.post(send_url, headers=headers, json=body)
    send_res = r.json()
    logger.info(f"Send audio message result: {send_res}")
    return send_res.get("code") == 0

def send_feishu_text(open_id, text):
    if not open_id:
        logger.warning("send_feishu_text called but open_id is empty!")
        return False
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    send_url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    body = {
        "receive_id": open_id,
        "msg_type": "text",
        "content": json.dumps({"text": text})
    }
    try:
        r = requests.post(send_url, headers=headers, json=body, timeout=5)
        logger.info(f"Send text to {open_id} result: {r.json()}")
        return r.json().get("code") == 0
    except Exception as e:
        logger.error(f"Failed to send text to Feishu: {e}")
        return False

# 低电量通知状态机（带迟滞回差，防重复轰炸）
g_battery_warn_state = 0  # 0: 正常, 1: 已报15%, 2: 已报5%

def check_and_notify_battery(batt_pct, charging):
    global g_battery_warn_state, current_open_id
    if not current_open_id:
        return

    # 迟滞回差：只要充回 30% 以上，或者正在充电且 >= 20%，就解除报警锁定
    if charging or batt_pct >= 30:
        if g_battery_warn_state != 0 and batt_pct >= 30:
            logger.info("Battery recovered >= 30%. Resetting low-battery alert state machine.")
            g_battery_warn_state = 0
        return

    # 未充电情况下的分级单次提醒
    if batt_pct <= 5 and g_battery_warn_state < 2:
        g_battery_warn_state = 2
        text = f"🔴【紧急电量告警】宝贝的留言机电量仅剩 {batt_pct}%！即将自动关机休眠防过放，墨水屏已锁定充电图标，请尽快帮宝贝插上充电线！"
        logger.warning(f"Triggering critical battery notification: {text}")
        send_feishu_text(current_open_id, text)
    elif batt_pct <= 15 and g_battery_warn_state < 1:
        g_battery_warn_state = 1
        text = f"🔋【电量提醒】宝贝的留言机当前电量为 {batt_pct}%，今晚记得帮他充上电哦~"
        logger.info(f"Triggering 15% battery notification: {text}")
        send_feishu_text(current_open_id, text)

# --- Feishu Event Handler ---
def on_feishu_message(data: lark.im.v1.P2ImMessageReceiveV1):
    global current_open_id, server_loop
    msg = data.event.message
    sender = data.event.sender
    current_open_id = sender.sender_id.open_id
    msg_id = msg.message_id
    logger.info(f"Received Feishu message: type={msg.message_type}, from={current_open_id}, msg_id={msg_id}")

    if msg.message_type == "audio":
        content = json.loads(msg.content)
        file_key = content.get("file_key")
        tmp_download = DATA_DIR / f"{file_key}.audio"
        msg_wav_path = DATA_DIR / f"msg_{msg_id}.wav"
        logger.info(f"Downloading voice file_key: {file_key}")
        if download_feishu_resource(msg.message_id, file_key, tmp_download):
            convert_to_wav(tmp_download, msg_wav_path)
            logger.info("Voice converted to 16kHz mono WAV successfully!")
            
            unread_messages.append({
                "msg_id": msg_id,
                "audio_path": msg_wav_path,
                "text": "语音留言",
                "timestamp": int(time.time())
            })

            render_child_screen("new_message")

            try:
                fut = asyncio.run_coroutine_threadsafe(
                    notify_device({
                        "type": "new_audio",
                        "unread_count": len(unread_messages),
                        "url": "/api/audio/latest.wav",
                        "screen_url": "/api/screen/latest.bin",
                        "timestamp": int(time.time()),
                        "message_id": msg_id
                    }),
                    server_loop
                )
                delivered = fut.result(timeout=5)
                if delivered:
                    clear_and_set_message_reaction(msg_id, "CheckMark")
            except Exception as e:
                logger.error(f"Failed to notify device: {e}")

    elif msg.message_type == "text":
        content = json.loads(msg.content)
        text = content.get("text", "")
        logger.info(f"Text message: {text}")

        msg_wav_path = DATA_DIR / f"msg_{msg_id}.wav"
        try:
            fut = executor.submit(sync_generate_tts, text, msg_wav_path)
            fut.result(timeout=10)
            logger.info("Text-to-Speech generated WAV successfully!")

            unread_messages.append({
                "msg_id": msg_id,
                "audio_path": msg_wav_path,
                "text": text,
                "timestamp": int(time.time())
            })

            render_child_screen("new_message")

            fut_notify = asyncio.run_coroutine_threadsafe(
                notify_device({
                    "type": "new_audio",
                    "unread_count": len(unread_messages),
                    "url": "/api/audio/latest.wav",
                    "screen_url": "/api/screen/latest.bin",
                    "timestamp": int(time.time()),
                    "message_id": msg_id
                }),
                server_loop
            )
            delivered = fut_notify.result(timeout=5)
            if delivered:
                clear_and_set_message_reaction(msg_id, "CheckMark")
        except Exception as e:
            logger.error(f"TTS / notify failed: {e}")

def dummy_handler(data):
    pass

# --- HTTP & WebSocket Server ---
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    device_ws_clients.add(ws)
    logger.info(f"ESP32 device connected via WebSocket! (Total: {len(device_ws_clients)})")
    
    await ws.send_str(json.dumps({"type": "connected", "msg": "Welcome to Feishu Bridge"}))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                logger.info(f"Device says: {msg.data.strip()}")
                try:
                    payload = json.loads(msg.data.strip())
                    action = payload.get("action")
                    if "battery" in payload:
                        device_state["battery_pct"] = payload["battery"]
                    if "charging" in payload:
                        device_state["charging"] = payload["charging"]
                    if "volume" in payload:
                        device_state["volume_level"] = payload["volume"]
                    if "rssi" in payload:
                        device_state["wifi_rssi"] = payload["rssi"]

                    if action == "played":
                        if unread_messages:
                            popped = unread_messages.pop(0)
                            played_msg_id = popped["msg_id"]
                            clear_and_set_message_reaction(played_msg_id, "HEART")
                            logger.info(f"Message {played_msg_id} marked as played. Remaining unread: {len(unread_messages)}")

                        if len(unread_messages) > 0:
                            render_child_screen("new_message")
                        else:
                            render_child_screen("idle")

                        await notify_device({
                            "type": "refresh_screen",
                            "unread_count": len(unread_messages),
                            "screen_url": "/api/screen/latest.bin"
                        })

                    elif action == "recording_start":
                        render_child_screen("recording")
                        await notify_device({
                            "type": "refresh_screen",
                            "screen_url": "/api/screen/latest.bin"
                        })
                    elif action == "status_report":
                        batt = payload.get("battery", 100)
                        chg = payload.get("charging", False)
                        check_and_notify_battery(batt, chg)
                        # 状态上报仅在云端记录和做低电量告警，绝不反向命令墨水屏刷新！
                        logger.info(f"Recorded status report: batt={batt}%, chg={chg}")
                except Exception as e:
                    logger.error(f"Error parsing device message: {e}")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"WS connection closed with error {ws.exception()}")
    finally:
        device_ws_clients.remove(ws)
        logger.info("ESP32 device disconnected from WebSocket.")
    return ws

async def handle_get_audio(request):
    if not unread_messages:
        fallback_files = sorted(DATA_DIR.glob("msg_*.wav"), key=os.path.getmtime, reverse=True)
        if fallback_files:
            return web.FileResponse(fallback_files[0])
        return web.Response(status=404, text="No audio available")
    
    current_item = unread_messages[0]
    return web.FileResponse(current_item["audio_path"])

async def handle_get_screen(request):
    if not latest_epd_img_path.exists():
        return web.Response(status=404, text="No screen bitmap available")
    return web.FileResponse(latest_epd_img_path)

async def handle_upload_audio(request):
    global current_open_id
    data = await request.read()
    if not data:
        return web.json_response({"error": "Empty audio data"}, status=400)
    
    upload_file = DATA_DIR / f"child_record_{int(time.time())}.wav"
    with open(upload_file, "wb") as f:
        f.write(data)
    logger.info(f"Received {len(data)} bytes audio from ESP32. Saved to {upload_file}")

    if len(unread_messages) > 0:
        render_child_screen("new_message")
    else:
        render_child_screen("idle")

    await notify_device({
        "type": "refresh_screen",
        "unread_count": len(unread_messages),
        "screen_url": "/api/screen/latest.bin"
    })

    if not current_open_id:
        return web.json_response({"status": "no_recipient", "msg": "Please send a message from Feishu first to pair"})
    
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, upload_and_send_voice_to_feishu, upload_file, current_open_id)
    if success:
        return web.json_response({"status": "ok", "msg": "Sent to Feishu"})
    else:
        return web.json_response({"status": "failed", "msg": "Upload to Feishu failed"}, status=500)

async def start_http_app():
    app = web.Application()
    app.router.add_get("/ws/device", ws_handler)
    app.router.add_get("/api/audio/latest.wav", handle_get_audio)
    app.router.add_get("/api/screen/latest.bin", handle_get_screen)
    app.router.add_post("/api/upload_audio", handle_upload_audio)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("HTTP & WebSocket server running on http://0.0.0.0:8080")

def main():
    global server_loop
    server_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(server_loop)

    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_feishu_message) \
        .register_p2_im_message_reaction_created_v1(dummy_handler) \
        .register_p2_im_message_reaction_deleted_v1(dummy_handler) \
        .build()

    ws_client = lark.ws.Client(APP_ID, APP_SECRET, event_handler=event_handler, log_level=lark.LogLevel.INFO)
    import threading
    t = threading.Thread(target=ws_client.start, daemon=True)
    t.start()
    logger.info("Feishu WebSocket thread launched.")

    server_loop.run_until_complete(start_http_app())
    try:
        server_loop.run_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
