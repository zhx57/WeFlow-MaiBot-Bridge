from unittest.mock import Mock, call

import pytest

from weflow_maibot_bridge.uia import UIASendError, UIASender


class FakeWin32:
    def __init__(self, foreground: bool = True) -> None:
        self.foreground = foreground
        self.user32 = Mock()
        self.user32.GetForegroundWindow.return_value = 200

    def foreground_is(self, _hwnd: int) -> bool:
        return self.foreground

    @staticmethod
    def root(hwnd: int) -> int:
        return hwnd


def focused_control(hwnd: int = 100) -> Mock:
    control = Mock()
    top = Mock()
    top.NativeWindowHandle = hwnd
    control.GetTopLevelControl.return_value = top
    control.ControlTypeName = "EditControl"
    return control


def rectangle(left, top, right, bottom):
    rect = Mock()
    rect.left = left
    rect.top = top
    rect.right = right
    rect.bottom = bottom
    rect.width.return_value = right - left
    rect.height.return_value = bottom - top
    return rect


def wechat_window() -> Mock:
    window = Mock()
    window.BoundingRectangle = rectangle(0, 0, 1000, 800)
    return window


def test_switch_contact_stops_before_keys_when_wechat_is_not_foreground() -> None:
    auto = Mock()
    with pytest.raises(UIASendError, match="失去前台焦点"):
        UIASender._switch_contact(auto, Mock(), Mock(), "Alice", 100, FakeWin32(False))
    auto.SendKeys.assert_not_called()


def test_switch_contact_uses_focused_wechat_search_control(monkeypatch) -> None:
    monkeypatch.setattr("weflow_maibot_bridge.uia.time.sleep", lambda _seconds: None)
    auto = Mock()
    focused = focused_control()
    focused.BoundingRectangle = rectangle(20, 20, 400, 80)
    auto.GetFocusedControl.return_value = focused
    clipboard = Mock()

    UIASender._switch_contact(auto, wechat_window(), clipboard, "Alice", 100, FakeWin32())

    assert auto.SendKeys.call_args_list == [call("{Ctrl}f"), call("{Enter}")]
    assert focused.SendKeys.call_args_list == [call("{Ctrl}a"), call("{Ctrl}v")]
    clipboard.copy.assert_called_once_with("Alice")


def test_require_input_focus_rejects_control_from_other_window() -> None:
    auto = Mock()
    input_control = focused_control(100)
    input_control.HasKeyboardFocus = True
    auto.GetFocusedControl.return_value = focused_control(200)

    with pytest.raises(UIASendError, match="输入框失去焦点"):
        UIASender._require_input_focus(auto, input_control, 100, FakeWin32())


def test_switch_contact_rejects_chat_input_as_search_box(monkeypatch) -> None:
    monkeypatch.setattr("weflow_maibot_bridge.uia.time.sleep", lambda _seconds: None)
    auto = Mock()
    focused = focused_control()
    focused.BoundingRectangle = rectangle(250, 500, 950, 760)
    auto.GetFocusedControl.return_value = focused

    with pytest.raises(UIASendError, match="搜索框没有获得焦点"):
        UIASender._switch_contact(auto, wechat_window(), Mock(), "Alice", 100, FakeWin32())
    assert auto.SendKeys.call_args_list == [call("{Ctrl}f")]


def test_window_exists_recovers_from_stale_uia_proxy() -> None:
    window = Mock()
    window.Exists.side_effect = RuntimeError("stale element")
    assert UIASender._window_exists(window) is False
