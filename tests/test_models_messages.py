from pathlib import Path

from maim_message import BaseMessageInfo, MessageBase, ReceiverInfo, Seg, UserInfo

from weflow_maibot_bridge.messages import build_message, resolve_reply_target
from weflow_maibot_bridge.models import (
    InboundMessage,
    InboundPart,
    event_message_id,
    is_group_message,
    normalize_event,
    policy_accept,
    stable_id,
)
from weflow_maibot_bridge.storage import Storage


def event(**changes):
    value = {
        "rawid": "raw-1",
        "timestamp": 100,
        "sessionId": "wxid-alice",
        "talkerId": "wxid-alice",
        "sourceName": "Alice",
        "content": "hello",
    }
    value.update(changes)
    return value


def test_stable_ids_prefer_rawid() -> None:
    assert stable_id("weflow", "user", "id") == stable_id("weflow", "user", "id")
    assert len(stable_id("weflow", "user", "id")) == 64
    first = event_message_id("weflow", event(content="one"))
    second = event_message_id("weflow", event(content="two"))
    assert first == second
    assert event_message_id("weflow", event(rawid="", _sse_event_id="event-1")) == event_message_id(
        "weflow", event(rawid="", _sse_event_id="event-1", content="different")
    )
    assert event_message_id("weflow", event(rawid="", timestamp=None, _received_at_ns=1)) != event_message_id(
        "weflow", event(rawid="", timestamp=None, _received_at_ns=2)
    )


def test_private_group_detection_and_policy() -> None:
    assert not is_group_message(event())
    group = event(sessionId="room@chatroom", sessionType="group", groupName="群", senderName="Bob", content="@Mai hi")
    assert is_group_message(group)
    message = normalize_event(group, "weflow", 1, ("Mai",), "wxid-bot")
    assert message and message.chat_type == "group" and message.mentioned
    assert policy_accept(message, "mention")
    message.mentioned = False
    assert not policy_accept(message, "mention")
    assert policy_accept(message, "all") and policy_accept(message, "batch")


def test_filter_self_voice_empty_and_structured_mention() -> None:
    assert normalize_event(event(sourceName="Mai"), "weflow", 1, ("Mai",), "bot") is None
    assert normalize_event(event(type=34), "weflow", 1, ("Mai",), "bot") is None
    assert normalize_event(event(content=""), "weflow", 1, ("Mai",), "bot") is None
    group = event(sessionId="g@chatroom", sessionType="group", mentions=[{"wxid": "bot"}])
    normalized = normalize_event(group, "weflow", 1, ("Mai",), "bot")
    assert normalized and normalized.mentioned
    own_group_message = event(
        sessionId="g@chatroom", talkerId="g@chatroom", sourceName="群聊",
        senderId="bot", content="机器人回复",
    )
    assert normalize_event(own_group_message, "weflow", 1, ("Mai",), "bot") is None


def test_messagebase_build_batch_preserves_members_and_reply_route(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite3", 10)
    storage.initialize()
    messages = [
        InboundMessage("m1", 1, 1, "group", "gid", "测试群", "u1", "甲", InboundPart("text", "一")),
        InboundMessage("m2", 2, 2, "group", "gid", "测试群", "u2", "乙", InboundPart("image", "aGVsbG8=")),
    ]
    built = build_message(messages, "weflow", "Mai", storage, group_batch=True)
    payload = built.to_dict()
    assert payload["message_info"]["platform"] == "weflow"
    assert payload["message_segment"]["type"] == "seglist"
    assert "甲" in payload["message_segment"]["data"][0]["data"]
    group_id = built.message_info.group_info.group_id
    reply = MessageBase(
        BaseMessageInfo(platform="weflow", receiver_info=ReceiverInfo(user_info=UserInfo(platform="weflow", user_id=group_id))),
        Seg("text", "ok"),
    )
    assert resolve_reply_target(reply, "weflow", storage) == ("测试群", "group")
