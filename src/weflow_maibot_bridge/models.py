from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal


ChatType = Literal["private", "group"]
MediaKind = Literal["image", "emoji"]


@dataclass(slots=True)
class InboundPart:
    type: Literal["text", "image", "emoji"]
    data: str
    caption: str | None = None


@dataclass(slots=True)
class InboundMessage:
    message_id: str
    sequence: int
    timestamp: float
    chat_type: ChatType
    session_key: str
    session_name: str
    sender_key: str
    sender_name: str
    part: InboundPart
    rawid: str = ""
    mentioned: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def stable_id(platform: str, kind: str, identifier: str) -> str:
    value = f"{platform}\x1f{kind}\x1f{identifier.strip()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def event_message_id(platform: str, data: dict[str, Any]) -> str:
    rawid = str(data.get("rawid") or data.get("rawId") or "").strip()
    if rawid:
        return stable_id(platform, "message", rawid)
    sse_event_id = str(data.get("_sse_event_id") or "").strip()
    if sse_event_id:
        return stable_id(platform, "sse-message", sse_event_id)
    canonical = {
        "session": data.get("sessionId") or data.get("talkerId"),
        "sender": data.get("senderId") or data.get("senderName") or data.get("sourceName"),
        "timestamp": data.get("timestamp"),
        "type": data.get("type") or data.get("msgType"),
        "content": data.get("content"),
    }
    received_at = data.get("_received_at_ns")
    if received_at is not None:
        canonical["received_at_ns"] = received_at
    return stable_id(platform, "message-fallback", json.dumps(canonical, sort_keys=True, ensure_ascii=False))


def is_group_message(data: dict[str, Any]) -> bool:
    session = str(data.get("sessionId") or "")
    return (
        str(data.get("sessionType") or "").lower() == "group"
        or bool(data.get("groupName"))
        or "@chatroom" in session
    )


def detect_mention(data: dict[str, Any], bot_nicknames: tuple[str, ...], bot_wxid: str) -> bool:
    for key in ("isMentioned", "atMe", "mentioned"):
        if data.get(key) is True:
            return True
    mentions = data.get("mentions") or data.get("atList") or []
    if isinstance(mentions, str):
        mentions = [mentions]
    if isinstance(mentions, list):
        values = {str(item.get("wxid") or item.get("id") or item.get("name")) if isinstance(item, dict) else str(item) for item in mentions}
        if bot_wxid and bot_wxid in values:
            return True
        if values.intersection(bot_nicknames):
            return True
    content = str(data.get("content") or "")
    return any(f"@{name}" in content or f"＠{name}" in content for name in bot_nicknames)


def normalize_event(
    data: dict[str, Any],
    platform: str,
    sequence: int,
    bot_nicknames: tuple[str, ...],
    bot_wxid: str,
) -> InboundMessage | None:
    content = str(data.get("content") or "").strip()
    msg_type = data.get("type") or data.get("msgType") or 0
    source = str(data.get("sourceName") or data.get("talkerName") or "").strip()
    talker_id = str(data.get("talkerId") or "").strip()
    sender_identity = str(
        data.get("senderId") or data.get("memberId") or data.get("senderWxid") or ""
    ).strip()
    if source in bot_nicknames or (
        bot_wxid and (talker_id == bot_wxid or sender_identity == bot_wxid)
    ):
        return None
    if msg_type == 34 or "[语音]" in content:
        return None
    media_type = str(data.get("mediaType") or "").lower()
    is_image = content == "[图片]" or media_type == "image"
    is_emoji = content in {"[动画表情]", "[表情]"} or media_type in {"sticker", "emoji"}
    if not content and not is_image and not is_emoji:
        return None

    group = is_group_message(data)
    chat_type: ChatType = "group" if group else "private"
    session_key = str(data.get("sessionId") or data.get("talkerId") or source).strip()
    session_name = str(data.get("groupName") or source or session_key).strip()
    if group:
        session_name = re.sub(r"\s*\(\d+\)\s*$", "", session_name).strip()
    sender_name = str(data.get("senderName") or data.get("sender") or source or "未知用户").strip()
    sender_key = str(data.get("senderId") or data.get("memberId") or data.get("senderWxid") or "").strip()
    if not sender_key:
        sender_key = f"{session_key}|{sender_name}" if group else (talker_id or session_key)
    if not session_key:
        return None
    part_type = "image" if is_image else "emoji" if is_emoji else "text"
    timestamp = float(data.get("timestamp") or time.time())
    if timestamp > 100_000_000_000:
        timestamp /= 1000.0
    return InboundMessage(
        message_id=event_message_id(platform, data),
        sequence=sequence,
        timestamp=timestamp,
        chat_type=chat_type,
        session_key=session_key,
        session_name=session_name,
        sender_key=sender_key,
        sender_name=sender_name,
        part=InboundPart(part_type, content),
        rawid=str(data.get("rawid") or data.get("rawId") or ""),
        mentioned=detect_mention(data, bot_nicknames, bot_wxid),
        raw=data,
    )


def policy_accept(message: InboundMessage, mode: str) -> bool:
    if message.chat_type == "private":
        return True
    if mode == "mention":
        return message.mentioned
    return mode in {"all", "batch"}
