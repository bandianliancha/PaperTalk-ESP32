import os
import lark_oapi as lark
import threading
import time

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    print(f"\n[RECEIVED FEISHU EVENT] message_id={msg.message_id}, type={msg.message_type}, content={msg.content}\n")

event_handler = lark.EventDispatcherHandler.builder("", "") \
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
    .build()

client = lark.ws.Client(APP_ID, APP_SECRET, event_handler=event_handler, log_level=lark.LogLevel.INFO)

t = threading.Thread(target=client.start, daemon=True)
t.start()
print("Lark WebSocket client thread started. Waiting 5s for connection status...")
time.sleep(5)
print("Finished 5s check.")
