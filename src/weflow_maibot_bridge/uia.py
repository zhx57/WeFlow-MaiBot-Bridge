from __future__ import annotations

import asyncio
import logging
import os
import platform
import queue
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from .config import UIAConfig


log = logging.getLogger(__name__)


class UIASendError(RuntimeError):
    def __init__(self, message: str, *, retry_safe: bool) -> None:
        super().__init__(message)
        self.retry_safe = retry_safe


@dataclass(slots=True)
class _Command:
    contact: str
    kind: str
    data: str
    future: Future[None]


class UIASender:
    """One dedicated COM/UIA thread; every WeChat operation is strictly serialized."""

    def __init__(self, config: UIAConfig) -> None:
        self.config = config
        self._commands: queue.Queue[_Command | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if self.config.dry_run:
            log.warning("UIA dry-run 已启用，微信出站只记录不操作")
            return
        if platform.system() != "Windows":
            raise RuntimeError("UIA 发送仅支持 Windows；非 Windows 验证请配置 uia.dry_run=true")
        self._thread = threading.Thread(target=self._worker, name="wechat-uia", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.config.operation_timeout):
            self._stopping.set()
            self._commands.put(None)
            raise TimeoutError("UIA 初始化超时")
        if self._startup_error:
            raise RuntimeError("UIA 初始化失败") from self._startup_error

    async def send(self, contact: str, kind: str, data: str) -> None:
        if not contact.strip():
            raise ValueError("微信发送目标为空")
        if self.config.dry_run:
            log.info("dry-run 微信发送 target=%s kind=%s", contact, kind)
            return
        future: Future[None] = Future()
        self._commands.put(_Command(contact, kind, data, future))
        try:
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=self.config.operation_timeout)
        except TimeoutError as exc:
            # Worker may already have pressed Enter. The caller must not retry.
            error = UIASendError("UIA 操作超时，发送结果未知", retry_safe=False)
            setattr(error, "command_future", future)
            raise error from exc

    async def stop(self) -> None:
        if not self._thread:
            return
        self._stopping.set()
        self._commands.put(None)
        await asyncio.to_thread(self._thread.join, self.config.operation_timeout)
        if self._thread.is_alive():
            log.error("UIA 工作线程未在时限内停止")

    def _worker(self) -> None:
        import ctypes

        initialized = False
        try:
            result = ctypes.windll.ole32.CoInitializeEx(None, 2)
            initialized = result in (0, 1)
            import pyperclip
            import uiautomation as auto

            window = self._find_window(auto)
            if window is None:
                raise RuntimeError("未找到微信 4.x 主窗口，请先启动并登录微信")
            self._ready.set()
            last_contact = ""
            while not self._stopping.is_set():
                command = self._commands.get()
                if command is None:
                    return
                started = False
                try:
                    if not window.Exists(0.5):
                        window = self._find_window(auto)
                    if window is None:
                        raise UIASendError("微信窗口已失效", retry_safe=True)
                    self._activate(window)
                    if self.config.search_enabled and command.contact != last_contact:
                        self._switch_contact(auto, window, pyperclip, command.contact)
                        last_contact = command.contact
                    if command.kind == "text":
                        pyperclip.copy(command.data)
                    elif command.kind == "image":
                        self._copy_image_to_clipboard(command.data)
                    else:
                        raise UIASendError(f"UIA 不支持发送类型 {command.kind}", retry_safe=True)
                    started = True
                    auto.SendKeys("{Ctrl}v")
                    time.sleep(min(2.0, 0.2 + len(command.data) * 0.005) if command.kind == "text" else 1.0)
                    auto.SendKeys("{Enter}")
                    command.future.set_result(None)
                except BaseException as exc:
                    error = exc if isinstance(exc, UIASendError) else UIASendError(
                        f"UIA 发送失败: {exc}", retry_safe=not started
                    )
                    if not command.future.done():
                        command.future.set_exception(error)
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            if initialized:
                ctypes.windll.ole32.CoUninitialize()

    def _find_window(self, auto):
        for window in auto.GetRootControl().GetChildren():
            name = str(getattr(window, "Name", ""))
            class_name = str(getattr(window, "ClassName", ""))
            if class_name in {"Chrome_WidgetWin_1", "CabinetWClass"}:
                continue
            if any(title in name for title in self.config.window_titles):
                return window
        return None

    @staticmethod
    def _activate(window) -> None:
        window.SetActive()
        time.sleep(0.25)
        if not window.Exists(0.2):
            raise UIASendError("微信窗口激活后失效", retry_safe=True)

    @staticmethod
    def _switch_contact(auto, window, clipboard, contact: str) -> None:
        auto.SendKeys("{Ctrl}f")
        time.sleep(0.4)
        auto.SendKeys("{Ctrl}a")
        clipboard.copy(contact)
        auto.SendKeys("{Ctrl}v")
        time.sleep(0.5)
        # Search text must be visible before selecting the first result.
        visible = any(contact == str(getattr(control, "Name", "")) for control in window.GetChildren())
        if not visible:
            visible = window.TextControl(searchDepth=12, Name=contact).Exists(0.8)
        if not visible:
            auto.SendKeys("{Esc}")
            raise UIASendError(f"微信搜索结果未验证: {contact}", retry_safe=True)
        auto.SendKeys("{Enter}")
        time.sleep(0.8)
        if not window.Exists(0.2):
            raise UIASendError("选择联系人后微信窗口失效", retry_safe=True)

    @staticmethod
    def _copy_image_to_clipboard(path_value: str) -> None:
        path = Path(path_value).resolve(strict=True)
        if not path.is_file():
            raise UIASendError("待发送图片不存在", retry_safe=True)
        env = os.environ.copy()
        env["WEFLOW_IMAGE_PATH"] = str(path)
        # Script is constant; the untrusted path is passed only through an environment variable.
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$img=[System.Drawing.Image]::FromFile($env:WEFLOW_IMAGE_PATH);"
            "try {[System.Windows.Forms.Clipboard]::SetImage($img)} finally {$img.Dispose()}"
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            env=env,
            check=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
