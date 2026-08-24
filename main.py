from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import GetMessageResourceRequest, P2ImMessageReceiveV1


LOG = logging.getLogger("feishu_file_mailer")


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "config.json 不是合法 JSON。"
            " 如果是 Windows 路径，请把反斜杠写成双反斜杠，"
            '例如 "C:\\\\FeishuDownloads"，'
            '或者直接写成 "C:/FeishuDownloads"。'
            f" 原始错误: {exc}"
        ) from exc


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("\x00", "")
    name = re.sub(r'[<>:"/\\\\|?*]+', "_", name)
    return name or "unnamed_file"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    index = 1
    while True:
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def is_valid_email(value: str) -> bool:
    _, addr = parseaddr(value)
    return "@" in addr and "." in addr.split("@")[-1]


@dataclass(slots=True)
class Config:
    app_id: str
    app_secret: str
    download_dir: Path
    recipient_email: str
    subject_template: str
    body_template: str
    shell_verb_keyword: str
    foxmail_window_title_regex: str
    startup_wait_seconds: float
    compose_wait_seconds: float
    send_hotkey: str
    dedupe_history_limit: int
    allowed_chat_ids: set[str]
    allowed_sender_open_ids: set[str]
    log_level: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        recipient_email = str(data["recipient_email"]).strip()
        if not is_valid_email(recipient_email):
            raise ValueError(f"recipient_email 非法: {recipient_email}")
        return cls(
            app_id=str(data["app_id"]).strip(),
            app_secret=str(data["app_secret"]).strip(),
            download_dir=Path(str(data["download_dir"])).expanduser(),
            recipient_email=recipient_email,
            subject_template=str(data.get("subject_template", "飞书文件转发 - {file_name}")),
            body_template=str(data.get("body_template", "文件名：{file_name}")),
            shell_verb_keyword=str(data.get("shell_verb_keyword", "foxmail")).strip(),
            foxmail_window_title_regex=str(data.get("foxmail_window_title_regex", ".*Foxmail.*")),
            startup_wait_seconds=float(data.get("startup_wait_seconds", 2)),
            compose_wait_seconds=float(data.get("compose_wait_seconds", 20)),
            send_hotkey=str(data.get("send_hotkey", "^({ENTER})")),
            dedupe_history_limit=int(data.get("dedupe_history_limit", 5000)),
            allowed_chat_ids={str(v) for v in data.get("allowed_chat_ids", []) if str(v).strip()},
            allowed_sender_open_ids={str(v) for v in data.get("allowed_sender_open_ids", []) if str(v).strip()},
            log_level=str(data.get("log_level", "INFO")).upper(),
        )


class StateStore:
    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.lock = threading.Lock()
        self._state = {"processed_message_ids": []}
        if path.exists():
            self._state = load_json(path)
        self._processed = set(self._state.get("processed_message_ids", []))

    def contains(self, message_id: str) -> bool:
        with self.lock:
            return message_id in self._processed

    def add(self, message_id: str) -> None:
        with self.lock:
            if message_id in self._processed:
                return
            ids = self._state.setdefault("processed_message_ids", [])
            ids.append(message_id)
            self._processed.add(message_id)
            if len(ids) > self.limit:
                overflow = len(ids) - self.limit
                del ids[:overflow]
                self._processed = set(ids)
            ensure_dir(self.path.parent)
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=False, indent=2)


class FeishuClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api_client = lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret).build()

    def download_message_file(
        self,
        message_id: str,
        file_key: str,
        file_name: str,
        download_dir: Path,
        resource_type: str = "file",
    ) -> Path:
        target = unique_path(download_dir / sanitize_filename(file_name))
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type(resource_type) \
            .build()
        response = self.api_client.im.v1.message_resource.get(request)
        if not response.success():
            raise RuntimeError(f"飞书文件下载失败: code={response.code} msg={response.msg}")
        ensure_dir(download_dir)
        with target.open("wb") as fh:
            fh.write(response.raw.content)
        return target


class FoxmailSender:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._last_launch_pid: int | None = None

    def send_file(self, file_path: Path, recipient_email: str, subject: str, body: str) -> None:
        import pythoncom
        import win32com.client
        from pywinauto.keyboard import send_keys

        pythoncom.CoInitialize()
        try:
            self._last_launch_pid = None
            self._invoke_shell_verb(win32com.client, file_path)
            time.sleep(self.config.startup_wait_seconds)
            self._paste_recipient_and_send(recipient_email, send_keys)
            send_keys(self.config.send_hotkey, with_spaces=True)
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def _invoke_shell_verb(self, win32com_client: Any, file_path: Path) -> None:
        if self._invoke_sendto_shortcut(win32com_client, file_path):
            return
        shell = win32com_client.Dispatch("Shell.Application")
        folder = shell.Namespace(str(file_path.parent))
        item = folder.ParseName(file_path.name)
        verbs = list(item.Verbs())
        keyword = self.config.shell_verb_keyword.lower()
        for verb in verbs:
            name = verb.Name.replace("&", "").strip().lower()
            if keyword in name:
                verb.DoIt()
                LOG.info("已调用 Shell 动作: %s", verb.Name)
                return
        available = [verb.Name for verb in verbs]
        raise RuntimeError(f"未找到包含关键字 {self.config.shell_verb_keyword!r} 的 Shell 动作，可用动作: {available}")

    def _invoke_sendto_shortcut(self, win32com_client: Any, file_path: Path) -> bool:
        appdata = os.environ.get("APPDATA", "").strip()
        if not appdata:
            return False
        sendto_dir = Path(appdata) / "Microsoft" / "Windows" / "SendTo"
        if not sendto_dir.exists():
            return False

        keyword = self.config.shell_verb_keyword.lower()
        candidates = sorted(sendto_dir.iterdir(), key=lambda item: item.name.lower())
        for candidate in candidates:
            if keyword not in candidate.name.lower():
                continue
            LOG.info("检测到 SendTo 项: %s", candidate)
            if candidate.suffix.lower() == ".lnk":
                shell = win32com_client.Dispatch("WScript.Shell")
                shortcut = shell.CreateShortCut(str(candidate))
                target = Path(shortcut.Targetpath) if shortcut.Targetpath else None
                arguments = shortcut.Arguments or ""
                if target and target.exists():
                    cmd = [str(target)]
                    if arguments:
                        cmd.extend(arguments.split())
                    cmd.append(str(file_path))
                    proc = subprocess.Popen(cmd)
                    self._last_launch_pid = proc.pid
                    LOG.info("已通过 SendTo 快捷方式目标启动 Foxmail: %s", target)
                    return True
            os.startfile(str(candidate))
            LOG.info("已打开 SendTo 项，尝试交由系统处理: %s", candidate)
            return True
        return False

    def _paste_recipient_and_send(self, recipient_email: str, send_keys: Any) -> None:
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(recipient_email)
        except Exception:
            LOG.warning("写入剪贴板失败，将继续尝试直接键盘输入", exc_info=True)
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
        time.sleep(1.0)
        try:
            send_keys("^v", with_spaces=True)
        except Exception:
            send_keys(recipient_email, with_spaces=True)
        time.sleep(0.5)


class FileRelayApp:
    def __init__(self, config: Config, runtime_dir: Path) -> None:
        self.config = config
        self.runtime_dir = runtime_dir
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.state = StateStore(runtime_dir / "state.json", config.dedupe_history_limit)
        self.feishu_client = FeishuClient(config)
        self.foxmail_sender = FoxmailSender(config)
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    def should_handle(self, event: dict[str, Any]) -> bool:
        message = event.get("message", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        chat_id = str(message.get("chat_id", "")).strip()
        sender_open_id = str(sender_id.get("open_id", "")).strip()
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            LOG.info("忽略消息，chat_id 不在允许列表中: %s", chat_id)
            return False
        if self.config.allowed_sender_open_ids and sender_open_id not in self.config.allowed_sender_open_ids:
            LOG.info("忽略消息，sender_open_id 不在允许列表中: %s", sender_open_id)
            return False
        return str(message.get("message_type", "")).strip().lower() in {"file", "image"}

    def on_message_event(self, data: P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        sender_id = data.event.sender.sender_id
        chat_id = str(msg.chat_id or "").strip()
        sender_open_id = str(sender_id.open_id or "").strip()
        print(f"收到飞书消息事件: type={msg.message_type}, message_id={msg.message_id}")
        LOG.info(
            "收到飞书消息事件: message_id=%s type=%s chat_id=%s chat_type=%s sender_open_id=%s",
            msg.message_id,
            msg.message_type,
            chat_id,
            msg.chat_type,
            sender_open_id,
        )
        event = {
            "message": {
                "message_id": msg.message_id,
                "chat_id": chat_id,
                "chat_type": msg.chat_type,
                "message_type": msg.message_type,
                "content": msg.content,
            },
            "sender": {"sender_id": {"open_id": sender_open_id}},
        }
        if not self.should_handle(event):
            LOG.info("该事件不是可处理的文件消息，已跳过")
            return
        message_id = str(msg.message_id or "").strip()
        if not message_id:
            LOG.warning("收到文件事件但缺少 message_id")
            return
        if self.state.contains(message_id):
            LOG.info("消息已处理，跳过: %s", message_id)
            return
        try:
            content = json.loads(msg.content)
            if str(msg.message_type).lower() == "image":
                file_key = str(content["image_key"])
                file_name = str(content.get("file_name") or f"{msg.message_id}.png")
                resource_type = "image"
            else:
                file_key = str(content["file_key"])
                file_name = str(content.get("file_name") or f"{file_key}.bin")
                resource_type = "file"
            print(f"捕获到文件消息: {file_name}，准备入队处理")
        except Exception:
            LOG.exception("文件消息内容解析失败: %s", msg.content)
            return
        with self._inflight_lock:
            if message_id in self._inflight:
                LOG.info("消息正在处理中，跳过重复事件: %s", message_id)
                return
            self._inflight.add(message_id)
        self.queue.put(
            {
                "message_id": message_id,
                "file_key": file_key,
                "file_name": file_name,
                "resource_type": resource_type,
                "sender_open_id": sender_open_id,
            }
        )
        LOG.info("文件事件已入队: %s", message_id)

    def worker_loop(self) -> None:
        while True:
            payload = self.queue.get()
            message_id = ""
            try:
                message_id = str(payload["message_id"])
                file_key = str(payload["file_key"])
                file_name = str(payload["file_name"])
                resource_type = str(payload.get("resource_type", "file"))
                sender_open_id = str(payload["sender_open_id"])
                local_path = self.feishu_client.download_message_file(
                    message_id=message_id,
                    file_key=file_key,
                    file_name=file_name,
                    download_dir=self.config.download_dir,
                    resource_type=resource_type,
                )
                print(f"文件已下载到: {local_path}")
                LOG.info("文件已下载: %s", local_path)
                subject = self.config.subject_template.format(
                    file_name=file_name,
                    message_id=message_id,
                    sender_open_id=sender_open_id,
                )
                body = self.config.body_template.format(
                    file_name=file_name,
                    message_id=message_id,
                    sender_open_id=sender_open_id,
                )
                self.foxmail_sender.send_file(
                    file_path=local_path,
                    recipient_email=self.config.recipient_email,
                    subject=subject,
                    body=body,
                )
                print("已触发 Foxmail 发送")
                LOG.info("Foxmail 已触发发送: %s", message_id)
                self.state.add(message_id)
            except Exception:
                LOG.exception("处理文件事件失败: %s", payload)
            finally:
                if message_id:
                    with self._inflight_lock:
                        self._inflight.discard(message_id)
                self.queue.task_done()

    def build_event_handler(self) -> Any:
        return lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self.on_message_event) \
            .build()

    def run(self) -> None:
        worker = threading.Thread(target=self.worker_loop, name="relay-worker", daemon=True)
        worker.start()
        event_handler = self.build_event_handler()
        client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )
        LOG.info("飞书长连接启动中")
        print("飞书文件转邮件程序已启动，等待接收文件...")
        client.start()


def setup_logging(runtime_dir: Path, level: str) -> None:
    ensure_dir(runtime_dir)
    log_file = runtime_dir / "app.log"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Feishu file messages and relay them by Foxmail.")
    parser.add_argument("--config", default="config.json", help="配置文件路径，默认 config.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    os.chdir(base_dir)
    config_path = Path(args.config).resolve()
    config = Config.from_dict(load_json(config_path))
    runtime_dir = ensure_dir(base_dir / "runtime")
    setup_logging(runtime_dir, config.log_level)
    ensure_dir(config.download_dir)
    LOG.info(
        "程序启动: download_dir=%s recipient_email=%s allowed_chat_ids=%s allowed_sender_open_ids=%s",
        config.download_dir,
        config.recipient_email,
        sorted(config.allowed_chat_ids),
        sorted(config.allowed_sender_open_ids),
    )
    app = FileRelayApp(config=config, runtime_dir=runtime_dir)
    app.run()


if __name__ == "__main__":
    main()
