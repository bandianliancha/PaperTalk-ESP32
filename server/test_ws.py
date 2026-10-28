import os
import sys
import lark_oapi as lark
from lark_channel import FeishuChannel
import asyncio

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

async def main():
    print(f"Connecting to Feishu Channel with App ID: {APP_ID}...")
    channel = FeishuChannel(app_id=APP_ID, app_secret=APP_SECRET)

    @channel.on("message")
    async def on_message(msg):
        print(f"--> Received message: {msg}")

    print("Starting channel connection loop for 5 seconds...")
    task = asyncio.create_task(channel.connect())
    await asyncio.sleep(5)
    print("Channel loop alive and healthy!")
    task.cancel()

asyncio.run(main())
