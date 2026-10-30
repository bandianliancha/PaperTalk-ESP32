import os
import sys
import time
import json
import math
import asyncio
import logging
import subprocess
import sqlite3
import threading
import wave
import array
import tempfile
import struct
from types import SimpleNamespace
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

DATA_DIR = Path(os.environ.get("VOICE_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("FeishuBridge")

device_ws_clients = set()
current_open_id = DEFAULT_OPEN_ID
latest_epd_img_path = DATA_DIR / "screen.bin"

# 持久化消息 ID，避免飞书重投递或服务重启后重复入队。
message_lock = threading.RLock()
message_db = sqlite3.connect(DATA_DIR / "messages.sqlite3", check_same_thread=False)
message_db.execute("CREATE TABLE IF NOT EXISTS messages (msg_id TEXT PRIMARY KEY, audio_path TEXT, text TEXT, timestamp INTEGER, status TEXT NOT NULL, message_type TEXT, content TEXT, sender_id TEXT)")
message_db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
message_db.execute("CREATE TABLE IF NOT EXISTS reply_sends (recording_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
reply_columns = {row[1] for row in message_db.execute("PRAGMA table_info(reply_sends)")}
for column, sql_type in (("recipient", "TEXT"), ("file_key", "TEXT"), ("first_send_at", "INTEGER")):
    if column not in reply_columns:
        message_db.execute(f"ALTER TABLE reply_sends ADD COLUMN {column} {sql_type}")
message_db.execute("UPDATE reply_sends SET status='unknown' WHERE status='sending'")
columns = {row[1] for row in message_db.execute("PRAGMA table_info(messages)")}
for column in ("message_type", "content", "sender_id"):
    if column not in columns:
        message_db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
# Older interrupted reservations have no event payload and cannot be recovered.
message_db.execute("DELETE FROM messages WHERE status='processing' AND content IS NULL")
message_db.commit()
saved_recipient = message_db.execute("SELECT value FROM metadata WHERE key='recipient'").fetchone()
if saved_recipient:
    current_open_id = saved_recipient[0]
unread_messages = [dict(zip(("msg_id", "audio_path", "text", "timestamp"), row)) for row in
    message_db.execute("SELECT msg_id, audio_path, text, timestamp FROM messages WHERE status = 'ready' ORDER BY rowid")]
for item in unread_messages:
    item["audio_path"] = Path(item["audio_path"])

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
message_executor = ThreadPoolExecutor(max_workers=1)
reaction_executor = ThreadPoolExecutor(max_workers=1)
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

def trim_silence(wav_path: Path):
    """Trim only the edges; preserve quiet consonants with generous padding."""
    with wave.open(str(wav_path), "rb") as source:
        params = source.getparams()
        if (params.nchannels, params.sampwidth, params.framerate) != (1, 2, 16000):
            raise ValueError("Expected 16 kHz mono PCM")
        pcm = source.readframes(params.nframes)
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    window = 800  # 50 ms RMS smooths out individual noise spikes
    levels = []
    for offset in range(0, len(samples), window):
        chunk = samples[offset:offset + window]
        if chunk:
            levels.append((offset, math.sqrt(sum(value * value for value in chunk) / len(chunk))))
    # Estimate a quiet background level from the recording itself. Only adapt
    # if there is a clear louder signal, and cap the threshold conservatively.
    threshold = 32768 * 10 ** (-48 / 20)
    if len(levels) >= 10:
        ordered = sorted(level for _, level in levels)
        noise_floor = ordered[len(ordered) // 5]
        if noise_floor > threshold and ordered[-1] > noise_floor * 8:
            threshold = min(32768 * 10 ** (-32 / 20), noise_floor * 1.8)
    active = [offset for offset, rms in levels if rms >= threshold]
    if not active:
        logger.info("Audio has no detected speech; keeping original to avoid deleting quiet speech")
        return
    start = max(0, active[0] - int(0.25 * params.framerate))
    end = min(len(samples), active[-1] + window + int(0.25 * params.framerate))
    with wave.open(str(wav_path), "wb") as output:
        output.setparams(params)
        output.writeframes(pcm[start * 2:end * 2])
    logger.info("Trimmed audio edges: %.2fs start, %.2fs end", start / params.framerate,
                (len(samples) - end) / params.framerate)

def convert_to_wav(src_path: Path, dst_wav_path: Path, denoise: bool = False):
    filters = []
    if denoise:
        # Reduce steady background noise; do not claim to separate all non-speech sounds.
        filters.extend(["highpass=f=70", "afftdn=nf=-48:nr=10"])
    filters.append("loudnorm=I=-14:TP=-1.0:LRA=11")
    cmd = ["ffmpeg", "-y", "-i", str(src_path), "-af", ",".join(filters),
           "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst_wav_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    trim_silence(dst_wav_path)

def convert_to_opus(src_wav: Path, dst_opus: Path):
    with tempfile.NamedTemporaryFile(suffix=".wav", dir=DATA_DIR) as temp:
        cleaned = Path(temp.name)
        convert_to_wav(src_wav, cleaned, denoise=True)
        cmd = ["ffmpeg", "-y", "-i", str(cleaned), "-c:a", "libopus", "-b:a", "16k",
               "-ar", "16000", "-ac", "1", str(dst_opus)]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def upload_and_send_voice_to_feishu(wav_path: Path, open_id: str, recording_id: str = "", existing_file_key: str = ""):
    if existing_file_key:
        return send_voice_file_to_feishu(open_id, existing_file_key, recording_id)
    opus_path = wav_path.with_suffix(".opus")
    convert_to_opus(wav_path, opus_path)
    
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(opus_path)]
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
        r = requests.post(upload_url, headers=headers, files=files, timeout=20)
        res = r.json()
        if res.get("code") != 0:
            logger.error(f"Feishu file upload failed: {res}")
            return False
        file_key = res["data"]["file_key"]
        logger.info(f"Uploaded audio to Feishu file_key: {file_key}")

    if recording_id:
        with message_lock:
            message_db.execute("UPDATE reply_sends SET file_key=? WHERE recording_id=?", (file_key, recording_id))
            message_db.commit()
    return send_voice_file_to_feishu(open_id, file_key, recording_id)

def send_voice_file_to_feishu(open_id: str, file_key: str, recording_id: str = ""):
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    if recording_id:
        with message_lock:
            message_db.execute("UPDATE reply_sends SET first_send_at=COALESCE(first_send_at, ?) WHERE recording_id=?", (int(time.time()), recording_id))
            message_db.commit()
    send_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    body = {"receive_id": open_id, "msg_type": "audio", "content": json.dumps({"file_key": file_key})}
    if recording_id:
        body["uuid"] = recording_id
    r = requests.post(send_url, headers=headers, json=body, timeout=20)
    send_res = r.json()
    logger.info("Send audio message result: %s", send_res)
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
def queue_snapshot():
    with message_lock:
        return {"unread_count": len(unread_messages),
                "head_id": unread_messages[0]["msg_id"] if unread_messages else ""}

async def broadcast_queue():
    await notify_device({"type": "queue_state", **queue_snapshot()})

def on_feishu_message(data: lark.im.v1.P2ImMessageReceiveV1):
    # Return promptly so Feishu can acknowledge the event before TTS/network work.
    msg = data.event.message
    sender = data.event.sender
    sender_id = getattr(getattr(sender, "sender_id", None), "open_id", None)
    sender_type = getattr(sender, "sender_type", None)
    if sender_type != "user" or not sender_id or msg.message_type not in ("text", "audio") or not msg.message_id:
        logger.info("Ignoring non-user or unsupported Feishu message: sender_type=%s type=%s", sender_type, msg.message_type)
        return
    with message_lock:
        try:
            message_db.execute("INSERT INTO messages (msg_id, status, message_type, content, sender_id) VALUES (?, 'processing', ?, ?, ?)", (msg.message_id, msg.message_type, msg.content, sender_id))
            message_db.commit()
        except sqlite3.IntegrityError:
            logger.info("Ignoring duplicate Feishu message %s", msg.message_id)
            return
    message_executor.submit(process_feishu_message, msg, sender_id)

def process_feishu_message(msg, sender_id):
    # Keep retries in the single worker, so later messages cannot overtake.
    for attempt in range(3):
        if process_feishu_message_once(msg, sender_id):
            return
        if attempt < 2:
            time.sleep(5)
    with message_lock:
        message_db.execute("UPDATE messages SET status='failed' WHERE msg_id=? AND status='processing'", (msg.message_id,))
        message_db.commit()
    logger.error("Message %s failed after 3 attempts; retaining it for diagnosis", msg.message_id)
    reaction_executor.submit(send_feishu_text, sender_id, "这条消息处理失败，设备未收到，请重新发送文字或语音。")

def prepare_playback_mp3(wav_path: Path):
    """Prepare 16 kHz, mono CBR frames; PCM remains the reliable fallback."""
    mp3_path = wav_path.with_suffix(".playback.mp3")
    temp_path = mp3_path.with_suffix(".tmp.mp3")
    try:
        mp3_path.unlink(missing_ok=True)
        with wave.open(str(wav_path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                raise ValueError("Unexpected audio format")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(wav_path),
                        "-map_metadata", "-1", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame",
                        "-b:a", "32k", "-write_xing", "0", "-id3v2_version", "0", str(temp_path)],
                       check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if not temp_path.stat().st_size or temp_path.stat().st_size % 144:
            raise ValueError("Unexpected MP3 frame length")
        temp_path.replace(mp3_path)
    except (OSError, subprocess.SubprocessError, wave.Error, ValueError):
        logger.exception("MP3 preparation failed; PCM remains available for %s", wav_path.name)
    finally:
        temp_path.unlink(missing_ok=True)

def process_feishu_message_once(msg, sender_id):
    global current_open_id
    msg_id = msg.message_id
    msg_wav_path = DATA_DIR / f"msg_{msg_id}.wav"
    committed = False
    try:
        content = json.loads(msg.content)
        if msg.message_type == "audio":
            file_key = content.get("file_key")
            if not file_key:
                raise ValueError("Audio message has no file_key")
            tmp_download = DATA_DIR / f"{file_key}.audio"
            if not download_feishu_resource(msg_id, file_key, tmp_download):
                raise RuntimeError("Audio download failed")
            convert_to_wav(tmp_download, msg_wav_path, denoise=True)
            text = "语音留言"
        else:
            text = content.get("text", "").strip()
            if not text:
                raise ValueError("Empty text message")
            sync_generate_tts(text, msg_wav_path)
        prepare_playback_mp3(msg_wav_path)
        item = {"msg_id": msg_id, "audio_path": msg_wav_path, "text": text, "timestamp": int(time.time())}
        with message_lock:
            message_db.execute("UPDATE messages SET audio_path=?, text=?, timestamp=?, status='ready' WHERE msg_id=?",
                               (str(msg_wav_path), text, item["timestamp"], msg_id))
            message_db.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('recipient', ?)", (sender_id,))
            message_db.commit()
            unread_messages.append(item)
            current_open_id = sender_id
            committed = True
        logger.info("Queued Feishu message %s; unread=%s", msg_id, queue_snapshot()["unread_count"])
        if server_loop:
            asyncio.run_coroutine_threadsafe(broadcast_queue(), server_loop)
        reaction_executor.submit(clear_and_set_message_reaction, msg_id, "CheckMark")
        return True
    except Exception:
        logger.exception("Failed to process Feishu message %s", msg_id)
        # If insertion already succeeded, never retry or enqueue twice.
        return committed

def recover_processing_messages():
    with message_lock:
        rows = message_db.execute("SELECT msg_id, message_type, content, sender_id FROM messages WHERE status='processing' ORDER BY rowid").fetchall()
    for msg_id, message_type, content, sender_id in rows:
        message_executor.submit(process_feishu_message, SimpleNamespace(message_id=msg_id, message_type=message_type, content=content), sender_id)

def dummy_handler(data):
    pass

# --- HTTP & WebSocket Server ---
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    device_ws_clients.add(ws)
    logger.info(f"ESP32 device connected via WebSocket! (Total: {len(device_ws_clients)})")
    
    await ws.send_str(json.dumps({"type": "queue_state", **queue_snapshot()}))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                raw_text = msg.data.strip()
                logger.info(f"Device says: {raw_text}")
                try:
                    # 容错解析：如果 ESP32 发来的文本存在粘包或尾随字符，提取第一段完整 JSON
                    try:
                        payload = json.loads(raw_text)
                    except json.JSONDecodeError:
                        decoder = json.JSONDecoder()
                        payload, _ = decoder.raw_decode(raw_text)

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
                        played_msg_id = payload.get("message_id")
                        removed = False
                        with message_lock:
                            if unread_messages and played_msg_id == unread_messages[0]["msg_id"]:
                                message_db.execute("UPDATE messages SET status='played' WHERE msg_id=? AND status='ready'", (played_msg_id,))
                                message_db.commit()
                                unread_messages.pop(0)
                                removed = True
                        if removed:
                            logger.info("Message %s marked played", played_msg_id)
                            reaction_executor.submit(clear_and_set_message_reaction, played_msg_id, "HEART")
                        else:
                            logger.info("Ignoring stale or unidentified playback acknowledgement: %s", played_msg_id)
                        await broadcast_queue()

                    elif action == "recording_start":
                        pass # 录音过程屏幕保持现状，不触发墨水屏刷新
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
    with message_lock:
        if not unread_messages:
            return web.Response(status=404, text="No unread audio")
        return web.FileResponse(unread_messages[0]["audio_path"])

async def handle_get_pcm(request):
    msg_id = request.match_info["message_id"]
    with message_lock:
        item = next((item for item in unread_messages if item["msg_id"] == msg_id), None)
        path = item["audio_path"] if item else None
    if path is None:
        return web.Response(status=404, text="Message not unread")
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                raise ValueError("Unexpected audio format")
            pcm = wav.readframes(wav.getnframes())
        return web.Response(body=pcm, content_type="application/octet-stream")
    except (OSError, wave.Error, ValueError):
        logger.exception("Cannot read audio for %s", msg_id)
        return web.Response(status=500, text="Invalid audio")

async def handle_get_mp3(request):
    msg_id = request.match_info["message_id"]
    with message_lock:
        item = next((item for item in unread_messages if item["msg_id"] == msg_id), None)
        path = item["audio_path"] if item else None
    if path is None:
        return web.Response(status=404, text="Message not unread")
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                raise ValueError("Unexpected audio format")
            samples = wav.getnframes()
        encoded = path.with_suffix(".playback.mp3").read_bytes()
        if not encoded or len(encoded) % 144:
            raise ValueError("Invalid MP3 frames")
        return web.Response(body=b"FMP1" + struct.pack("<I", samples) + encoded,
                            content_type="application/octet-stream")
    except (OSError, wave.Error, ValueError):
        return web.Response(status=404, text="MP3 unavailable")

async def handle_get_screen(request):
    if not latest_epd_img_path.exists():
        return web.Response(status=404, text="No screen bitmap available")
    return web.FileResponse(latest_epd_img_path)

async def handle_upload_audio(request):
    global current_open_id
    data = await request.read()
    if not data:
        return web.json_response({"status": "failed", "error": "Empty audio data"}, status=400)
    recording_id = request.headers.get("X-Recording-ID", "")
    if recording_id and (len(recording_id) > 50 or not recording_id.isalnum()):
        return web.json_response({"status": "failed", "error": "Invalid recording ID"}, status=400)
    if not current_open_id:
        return web.json_response({"status": "failed", "msg": "No recipient"}, status=400)
    existing_file_key = ""
    was_unknown = False
    recipient = current_open_id
    if recording_id:
        with message_lock:
            row = message_db.execute("SELECT status, recipient, file_key, first_send_at FROM reply_sends WHERE recording_id=?", (recording_id,)).fetchone()
            if row and row[0] == 'sent':
                return web.json_response({"status": "ok"})
            if row and row[0] == 'sending':
                return web.json_response({"status": "unknown"}, status=202)
            if row and row[0] == 'unknown':
                if row[1] and not row[3]:
                    # This version records first_send_at before any send, so
                    # absence of that marker means retry has not sent a message.
                    recipient = row[1]
                    existing_file_key = row[2] or ""
                elif row[1] and row[2] and row[3] and 0 <= time.time() - row[3] < 55 * 60:
                    recipient, existing_file_key, was_unknown = row[1], row[2], True
                else:
                    return web.json_response({"status": "unknown"}, status=202)
            elif row and row[1]:
                recipient = row[1]
            message_db.execute("INSERT INTO reply_sends (recording_id, status, recipient) VALUES (?, 'sending', ?) ON CONFLICT(recording_id) DO UPDATE SET status='sending'", (recording_id, recipient))
            message_db.commit()
    upload_file = DATA_DIR / (f"child_record_{recording_id}.wav" if recording_id else f"child_record_{time.time_ns()}.wav")
    try:
        upload_file.write_bytes(data)
        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, upload_and_send_voice_to_feishu, upload_file, recipient, recording_id, existing_file_key)
        status = 'sent' if success else ('unknown' if was_unknown else 'failed')
    except Exception:
        logger.exception("Reply send outcome unknown for %s", recording_id)
        status = 'unknown'
    if recording_id:
        with message_lock:
            message_db.execute("UPDATE reply_sends SET status=? WHERE recording_id=?", (status, recording_id))
            message_db.commit()
    if status == 'sent':
        return web.json_response({"status": "ok"})
    if status == 'unknown':
        return web.json_response({"status": "unknown"}, status=202)
    return web.json_response({"status": "failed"}, status=500)

async def start_http_app():
    app = web.Application()
    app.router.add_get("/ws/device", ws_handler)
    app.router.add_get("/api/audio/latest.wav", handle_get_audio)
    app.router.add_get("/api/audio/{message_id}.pcm", handle_get_pcm)
    app.router.add_get("/api/audio/{message_id}.mp3", handle_get_mp3)
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
    recover_processing_messages()
    try:
        server_loop.run_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
