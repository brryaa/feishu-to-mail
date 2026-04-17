from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetMessageResourceRequest, P2ImMessageReceiveV1

from main import Config, FoxmailSender, StateStore, ensure_dir, load_json, sanitize_filename, unique_path


BASE_DIR = Path(__file__).resolve().parent
CONFIG = Config.from_dict(load_json(BASE_DIR / "config.json"))
RUNTIME_DIR = ensure_dir(BASE_DIR / "runtime")
STATE = StateStore(RUNTIME_DIR / "state.json", CONFIG.dedupe_history_limit)
API_CLIENT = lark.Client.builder().app_id(CONFIG.app_id).app_secret(CONFIG.app_secret).build()
FOXMAIL = FoxmailSender(CONFIG)
INFLIGHT: set[str] = set()
INFLIGHT_LOCK = threading.Lock()


def process_file_task(message_id: str, file_key: str, file_name: str, sender_open_id: str) -> None:
    print(f"[1] 后台线程接手任务: {file_name}")
    request = GetMessageResourceRequest.builder() \
        .message_id(message_id) \
        .file_key(file_key) \
        .type("file") \
        .build()
    response = API_CLIENT.im.v1.message_resource.get(request)
    if not response.success():
        print(f"[X] 飞书下载失败: code={response.code} msg={response.msg}")
        with INFLIGHT_LOCK:
            INFLIGHT.discard(message_id)
        return

    ensure_dir(CONFIG.download_dir)
    target = unique_path(CONFIG.download_dir / sanitize_filename(file_name))
    with target.open("wb") as fh:
        fh.write(response.raw.content)
    print(f"[2] 文件落盘成功: {target}")

    time.sleep(max(CONFIG.startup_wait_seconds, 1))

    subject = CONFIG.subject_template.format(
        file_name=file_name,
        message_id=message_id,
        sender_open_id=sender_open_id,
    )
    body = CONFIG.body_template.format(
        file_name=file_name,
        message_id=message_id,
        sender_open_id=sender_open_id,
    )

    try:
        FOXMAIL.send_file(target, CONFIG.recipient_email, subject, body)
        STATE.add(message_id)
        print("[3] 已触发 Foxmail 发送")
    finally:
        with INFLIGHT_LOCK:
            INFLIGHT.discard(message_id)


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    sender_open_id = data.event.sender.sender_id.open_id
    print(f"收到飞书消息: type={msg.message_type}, id={msg.message_id}")

    if msg.message_type != "file":
        return

    if CONFIG.allowed_chat_ids and (msg.chat_id or "") not in CONFIG.allowed_chat_ids:
        print(f"跳过，chat_id 不在允许列表: {msg.chat_id}")
        return

    if CONFIG.allowed_sender_open_ids and (sender_open_id or "") not in CONFIG.allowed_sender_open_ids:
        print(f"跳过，sender_open_id 不在允许列表: {sender_open_id}")
        return

    if STATE.contains(msg.message_id):
        print(f"跳过，消息已处理: {msg.message_id}")
        return

    content = json.loads(msg.content)
    file_key = content.get("file_key")
    file_name = content.get("file_name", "unnamed_file")

    with INFLIGHT_LOCK:
        if msg.message_id in INFLIGHT:
            print(f"跳过，消息正在处理中: {msg.message_id}")
            return
        INFLIGHT.add(msg.message_id)

    print(f"主线程捕获文件消息: {file_name}，已分发给后台线程")
    task_thread = threading.Thread(
        target=process_file_task,
        args=(msg.message_id, file_key, file_name, sender_open_id),
        daemon=True,
    )
    task_thread.start()


EVENT_HANDLER = lark.EventDispatcherHandler.builder("", "") \
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
    .build()

WS_CLIENT = lark.ws.Client(
    CONFIG.app_id,
    CONFIG.app_secret,
    event_handler=EVENT_HANDLER,
    log_level=lark.LogLevel.INFO,
)


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    print("飞书文件转邮件兼容版已启动，等待接收文件...")
    WS_CLIENT.start()
