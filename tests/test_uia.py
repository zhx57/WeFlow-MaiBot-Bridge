from unittest.mock import Mock, call

from weflow_maibot_bridge.uia import UIASender


def test_switch_contact_uses_akasha_keyboard_flow_without_tree_validation(monkeypatch) -> None:
    monkeypatch.setattr("weflow_maibot_bridge.uia.time.sleep", lambda _seconds: None)
    auto = Mock()
    window = Mock()
    window.Exists.return_value = True
    clipboard = Mock()

    UIASender._switch_contact(auto, window, clipboard, "Alice")

    assert auto.SendKeys.call_args_list == [
        call("{Esc}"),
        call("{Ctrl}f"),
        call("{Ctrl}a"),
        call("{Ctrl}v"),
        call("{Enter}"),
    ]
    clipboard.copy.assert_called_once_with("Alice")
    window.TextControl.assert_not_called()
