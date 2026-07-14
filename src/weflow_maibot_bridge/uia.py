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


class _Win32:
    GA_ROOT = 2
    SW_RESTORE = 9

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def root(self, hwnd: int) -> int:
        if not hwnd:
            return 0
        root = self.user32.GetAncestor(hwnd, self.GA_ROOT)
        return int(root or hwnd)

    def foreground_is(self, hwnd: int) -> bool:
        return self.root(int(self.user32.GetForegroundWindow() or 0)) == self.root(hwnd)

    def window_hwnd(self, window) -> int:
        hwnd = int(getattr(window, "NativeWindowHandle", 0) or 0)
        if not hwnd:
            try:
                hwnd = int(getattr(window.GetTopLevelControl(), "NativeWindowHandle", 0) or 0)
            except Exception:
                hwnd = 0
        hwnd = self.root(hwnd)
        if not hwnd or not self.user32.IsWindow(hwnd):
            raise UIASendError("微信窗口没有有效 HWND", retry_safe=True)
        return hwnd

    def activate(self, window) -> int:
        hwnd = self.window_hwnd(window)
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, self.SW_RESTORE)

        current_tid = int(self.kernel32.GetCurrentThreadId())
        target_tid = int(self.user32.GetWindowThreadProcessId(hwnd, None))
        foreground = int(self.user32.GetForegroundWindow() or 0)
        foreground_tid = int(self.user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        attached: list[tuple[int, int]] = []

        for source, target in ((current_tid, foreground_tid), (current_tid, target_tid)):
            if source and target and source != target:
                if self.user32.AttachThreadInput(source, target, True):
                    attached.append((source, target))

        try:
            try:
                window.SetActive()
            except Exception:
                pass
            self.user32.ShowWindow(hwnd, self.SW_RESTORE)
            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)

            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if self.foreground_is(hwnd):
                    log.debug("微信已切换到前台 hwnd=0x%x", hwnd)
                    return hwnd
                time.sleep(0.05)
        finally:
            for source, target in reversed(attached):
                self.user32.AttachThreadInput(source, target, False)

        foreground = int(self.user32.GetForegroundWindow() or 0)
        raise UIASendError(
            f"无法将微信切换到前台，已取消按键（微信=0x{hwnd:x}，前台=0x{foreground:x}）",
            retry_safe=True,
        )


class UIASender:
    """Run every WeChat UI operation on one COM/UIA thread."""

    def __init__(self, config: UIAConfig) -> None:
        self.config = config
        self._commands: queue.Queue[_Command | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopping = threading.Event()
        self._start_lock = threading.Lock()
        self._poisoned = False

    def start(self) -> None:
        if self.config.dry_run:
            log.warning("UIA dry-run 已启用，微信出站只记录不操作")
            return
        if platform.system() != "Windows":
            raise RuntimeError("UIA 发送仅支持 Windows；非 Windows验证请配置 uia.dry_run=true")
        with self._start_lock:
            if self._poisoned:
                raise RuntimeError("UIA 工作线程曾经超时，无法安全恢复，请重启 Bridge")
            if self._thread and self._thread.is_alive() and self._ready.is_set() and not self._startup_error:
                return
            if self._thread and self._thread.is_alive():
                self._stopping.set()
                self._commands.put(None)
                self._thread.join(self.config.operation_timeout)
                if self._thread.is_alive():
                    self._poisoned = True
                    raise RuntimeError("旧 UIA 工作线程仍未退出，请重启 Bridge")
            self._commands = queue.Queue()
            self._ready.clear()
            self._startup_error = None
            self._stopping.clear()
            self._thread = threading.Thread(target=self._worker, name="wechat-uia", daemon=True)
            self._thread.start()
            if not self._ready.wait(self.config.operation_timeout):
                self._stopping.set()
                self._commands.put(None)
                self._poisoned = True
                raise TimeoutError("UIA 初始化超时，请重启 Bridge")
            if self._startup_error:
                raise RuntimeError("UIA 初始化失败") from self._startup_error

    async def send(self, contact: str, kind: str, data: str) -> None:
        if not contact.strip():
            raise ValueError("微信发送目标为空")
        if self.config.dry_run:
            log.info("dry-run 微信发送 target=%s kind=%s", contact, kind)
            return
        if self._poisoned:
            raise UIASendError("UIA 已卡死，请重启 Bridge", retry_safe=False)
        if not self._thread or not self._thread.is_alive() or self._startup_error or self._stopping.is_set():
            await asyncio.to_thread(self.start)

        future: Future[None] = Future()
        self._commands.put(_Command(contact, kind, data, future))
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=self.config.operation_timeout,
            )
        except TimeoutError as exc:
            self._poisoned = True
            raise UIASendError(
                "UIA 操作超时，发送结果未知；为避免继续误操作，请重启 Bridge",
                retry_safe=False,
            ) from exc

    async def stop(self) -> None:
        if not self._thread:
            return
        self._stopping.set()
        self._commands.put(None)
        await asyncio.to_thread(self._thread.join, self.config.operation_timeout)
        if self._thread.is_alive():
            self._poisoned = True
            log.error("UIA 工作线程未停止；请结束 Bridge 进程后重新启动")

    def _worker(self) -> None:
        import ctypes

        initialized = False
        try:
            result = ctypes.windll.ole32.CoInitializeEx(None, 2)
            if result not in (0, 1):
                raise RuntimeError(f"COM 初始化失败 HRESULT={result}")
            initialized = True
            import pyperclip
            import uiautomation as auto

            win32 = _Win32()
            window = self._find_window(auto, win32)
            if window is None:
                raise RuntimeError("未找到微信 4.x 主窗口，请先启动并登录微信")
            log.info(
                "找到微信窗口: %s class=%s hwnd=0x%x",
                getattr(window, "Name", ""),
                getattr(window, "ClassName", ""),
                win32.window_hwnd(window),
            )
            self._ready.set()

            while not self._stopping.is_set():
                command = self._commands.get()
                if command is None:
                    return
                if command.future.cancelled():
                    continue
                started = False
                try:
                    if not self._window_exists(window):
                        window = self._find_window(auto, win32)
                    if window is None:
                        raise UIASendError("微信窗口已失效", retry_safe=True)

                    hwnd = win32.activate(window)
                    if self.config.search_enabled:
                        self._switch_contact(auto, window, pyperclip, command.contact, hwnd, win32)
                    input_control = self._focus_chat_input(auto, window, hwnd, win32)

                    if command.kind == "text":
                        pyperclip.copy(command.data)
                    elif command.kind == "image":
                        self._copy_image_to_clipboard(command.data)
                    else:
                        raise UIASendError(f"UIA 不支持发送类型 {command.kind}", retry_safe=True)

                    self._require_input_focus(auto, input_control, hwnd, win32)
                    started = True
                    auto.SendKeys("{Ctrl}v")
                    wait = min(2.0, 0.25 + len(command.data) * 0.005) if command.kind == "text" else 1.2
                    time.sleep(wait)
                    self._require_input_focus(auto, input_control, hwnd, win32)
                    auto.SendKeys("{Enter}")
                    log.info("微信回复已执行 target=%s kind=%s", command.contact, command.kind)
                    if not command.future.done():
                        command.future.set_result(None)
                except BaseException as exc:
                    error = exc if isinstance(exc, UIASendError) else UIASendError(
                        f"UIA 发送失败: {exc}", retry_safe=not started
                    )
                    log.warning("微信 UIA 发送失败 target=%s: %s", command.contact, error)
                    if not command.future.done():
                        command.future.set_exception(error)
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            if initialized:
                ctypes.windll.ole32.CoUninitialize()

    def _find_window(self, auto, win32: _Win32):
        candidates = []
        for window in auto.GetRootControl().GetChildren():
            try:
                hwnd = win32.window_hwnd(window)
                class_name = str(getattr(window, "ClassName", ""))
                name = str(getattr(window, "Name", ""))
                class_match = class_name in {"Qt51514QWindowIcon", "WeChatMainWndForPC"}
                title_match = any(title in name for title in self.config.window_titles)
                if not class_match and not title_match:
                    continue
                rect = window.BoundingRectangle
                area = max(0, rect.width()) * max(0, rect.height()) if rect else 0
                if area > 0:
                    candidates.append((class_match, area, window, hwnd))
            except Exception:
                continue
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    @staticmethod
    def _window_exists(window) -> bool:
        if window is None:
            return False
        try:
            return bool(window.Exists(0.5))
        except Exception:
            return False

    @staticmethod
    def _require_foreground(hwnd: int, win32: _Win32) -> None:
        if not win32.foreground_is(hwnd):
            foreground = int(win32.user32.GetForegroundWindow() or 0)
            raise UIASendError(
                f"微信失去前台焦点，已取消按键（微信=0x{hwnd:x}，前台=0x{foreground:x}）",
                retry_safe=True,
            )

    @classmethod
    def _switch_contact(cls, auto, window, clipboard, contact: str, hwnd: int, win32: _Win32) -> None:
        cls._require_foreground(hwnd, win32)
        auto.SendKeys("{Ctrl}f")
        time.sleep(0.5)
        cls._require_foreground(hwnd, win32)
        focused = auto.GetFocusedControl()
        cls._require_control_in_window(focused, hwnd, win32, "微信搜索框没有获得焦点")
        if not cls._control_in_region(focused, window, "search"):
            raise UIASendError("Ctrl+F 后微信搜索框没有获得焦点，已取消发送", retry_safe=True)
        focused.SendKeys("{Ctrl}a")
        clipboard.copy(contact)
        cls._require_foreground(hwnd, win32)
        focused.SendKeys("{Ctrl}v")
        time.sleep(0.6)
        cls._require_control_in_window(auto.GetFocusedControl(), hwnd, win32, "微信搜索时焦点丢失")
        auto.SendKeys("{Enter}")
        time.sleep(0.9)
        cls._require_foreground(hwnd, win32)
        log.info("已在微信中打开聊天: %s", contact)

    @classmethod
    def _focus_chat_input(cls, auto, window, hwnd: int, win32: _Win32):
        focused = auto.GetFocusedControl()
        try:
            cls._require_control_in_window(focused, hwnd, win32, "微信当前焦点不在主窗口")
            focused_type = str(getattr(focused, "ControlTypeName", ""))
            if focused_type in {"EditControl", "DocumentControl"} and cls._control_in_region(
                focused, window, "input"
            ):
                log.debug("使用微信当前已聚焦的聊天输入框 type=%s", focused_type)
                return focused
        except UIASendError:
            pass

        win_rect = window.BoundingRectangle
        candidates = []

        def walk(control, depth: int = 0) -> None:
            if depth > 14:
                return
            try:
                for child in control.GetChildren():
                    try:
                        control_type = str(getattr(child, "ControlTypeName", ""))
                        rect = child.BoundingRectangle
                        if control_type in {"EditControl", "DocumentControl"} and rect:
                            if cls._control_in_region(child, window, "input"):
                                candidates.append((rect.width() * rect.height(), child))
                        walk(child, depth + 1)
                    except Exception:
                        continue
            except Exception:
                return

        walk(window)
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, control in candidates:
            try:
                control.SetFocus()
                time.sleep(0.1)
                if not bool(getattr(control, "HasKeyboardFocus", False)):
                    control.Click(simulateMove=False, waitTime=0)
                    time.sleep(0.1)
                cls._require_input_focus(auto, control, hwnd, win32)
                return control
            except Exception:
                continue
        raise UIASendError("未找到微信聊天输入框，已取消发送", retry_safe=True)

    @staticmethod
    def _control_in_region(control, window, region: str) -> bool:
        try:
            rect = control.BoundingRectangle
            win_rect = window.BoundingRectangle
            if not rect or not win_rect or rect.width() < 50 or rect.height() < 20:
                return False
            relative_left = (rect.left - win_rect.left) / max(1, win_rect.width())
            relative_top = (rect.top - win_rect.top) / max(1, win_rect.height())
            relative_right = (rect.right - win_rect.left) / max(1, win_rect.width())
            relative_bottom = (rect.bottom - win_rect.top) / max(1, win_rect.height())
            if region == "search":
                return relative_left < 0.5 and relative_top < 0.35 and relative_bottom < 0.5
            if region == "input":
                return (
                    relative_left >= 0.18
                    and relative_top >= 0.5
                    and relative_right > 0.45
                    and relative_bottom > 0.65
                )
        except Exception:
            return False
        return False

    @staticmethod
    def _require_control_in_window(control, hwnd: int, win32: _Win32, message: str) -> None:
        if control is None or not win32.foreground_is(hwnd):
            raise UIASendError(message, retry_safe=True)
        try:
            control_hwnd = int(getattr(control.GetTopLevelControl(), "NativeWindowHandle", 0) or 0)
        except Exception:
            control_hwnd = 0
        if win32.root(control_hwnd) != win32.root(hwnd):
            raise UIASendError(message, retry_safe=True)

    @classmethod
    def _require_input_focus(cls, auto, control, hwnd: int, win32: _Win32) -> None:
        cls._require_foreground(hwnd, win32)
        focused = auto.GetFocusedControl()
        cls._require_control_in_window(focused, hwnd, win32, "微信聊天输入框失去焦点")
        if not bool(getattr(control, "HasKeyboardFocus", False)):
            try:
                same = auto.ControlsAreSame(control, focused)
            except Exception:
                same = False
            if not same:
                raise UIASendError("微信聊天输入框失去焦点", retry_safe=True)

    @staticmethod
    def _copy_image_to_clipboard(path_value: str) -> None:
        path = Path(path_value).resolve(strict=True)
        if not path.is_file():
            raise UIASendError("待发送图片不存在", retry_safe=True)
        env = os.environ.copy()
        env["WEFLOW_IMAGE_PATH"] = str(path)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$img=[System.Drawing.Image]::FromFile($env:WEFLOW_IMAGE_PATH);"
            "try {[System.Windows.Forms.Clipboard]::SetImage($img)} finally {$img.Dispose()}"
        )
        subprocess.run(
            ["powershell.exe", "-STA", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            env=env,
            check=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
