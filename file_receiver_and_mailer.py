from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from main import Config, FoxmailSender, load_json, sanitize_filename, unique_path


BASE_DIR = Path(__file__).resolve().parent
CONFIG = Config.from_dict(load_json(BASE_DIR / "config.json"))

APP_ID = CONFIG.app_id
APP_SECRET = CONFIG.app_secret
SAVE_DIR = str(CONFIG.download_dir)

api_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
foxmail_sender = FoxmailSender(CONFIG)


def process_file_task(msg_id: str, file_key: str, file_name: str, sender_open_id: str) -> None:
    print(f"\n[1] 后台线程接手任务: {file_name}")

    request = GetMessageResourceRequest.builder() \
        .message_id(msg_id) \
        .file_key(file_key) \
        .type("file") \
        .build()

    response = api_client.im.v1.message_resource.get(request)

    if response.success():
        if not os.path.exists(SAVE_DIR):
            os.makedirs(SAVE_DIR)
        target = unique_path(Path(SAVE_DIR) / sanitize_filename(file_name))

        with open(target, "wb") as f:
            f.write(response.raw.content)
        print(f"[2] 文件落盘成功: {target}")

        print("⏳ 等待 2 秒，确保磁盘释放...")
        time.sleep(2)

        subject = CONFIG.subject_template.format(
            file_name=file_name,
            message_id=msg_id,
            sender_open_id=sender_open_id,
        )
        body = CONFIG.body_template.format(
            file_name=file_name,
            message_id=msg_id,
            sender_open_id=sender_open_id,
        )

        print("[3] 正在调用 Foxmail 发送邮件...")
        foxmail_sender.send_file(
            file_path=target,
            recipient_email=CONFIG.recipient_email,
            subject=subject,
            body=body,
        )
        print("[4] 邮件发送动作已触发")
    else:
        print(f"❌ 下载失败: {response.msg} (code={response.code})")


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    sender_open_id = data.event.sender.sender_id.open_id

    print(f"收到飞书消息: type={msg.message_type}, id={msg.message_id}")

    if msg.message_type == "file":
        content_dict = json.loads(msg.content)
        file_key = content_dict.get("file_key")
        file_name = content_dict.get("file_name", "unnamed_file")

        print(f"⚡ 主程序捕获事件: {file_name}，已分发给后台线程。")

        task_thread = threading.Thread(
            target=process_file_task,
            args=(msg.message_id, file_key, file_name, sender_open_id),
            daemon=True,
        )
        task_thread.start()


event_handler = lark.EventDispatcherHandler.builder("", "") \
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
    .build()

ws_client = lark.ws.Client(
    APP_ID,
    APP_SECRET,
    event_handler=event_handler,
    log_level=lark.LogLevel.INFO,
)


if __name__ == "__main__":
    print("🚀 飞书文件转邮件版（基于参考程序改造）已启动")
    ws_client.start()
