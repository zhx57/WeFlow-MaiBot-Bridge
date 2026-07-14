from __future__ import annotations

import hashlib
import time
from typing import Any

from maim_message.message_base import (
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    ReceiverInfo,
    Seg,
    SenderInfo,
    UserInfo,
)

from .models import InboundMessage, stable_id
from .storage import Storage


def build_message(
    batch: list[InboundMessage],
    platform: str,
    bot_name: str,
    storage: Storage,
    *,
    group_batch: bool = False,
) -> MessageBase:
    if not batch:
        raise ValueError("不能构造空消息批次")
    first = batch[0]
    if any(item.session_key != first.session_key or item.chat_type != first.chat_type for item in batch):
        raise ValueError("同一批次必须属于同一微信会话")
    group = None
    if first.chat_type == "group":
        group_id = stable_id(platform, "group", first.session_key)
        group = GroupInfo(platform=platform, group_id=group_id, group_name=first.session_name)
        storage.remember(group_id, first.session_name, "group")
    user_id = stable_id(platform, "user", first.sender_key)
    user = UserInfo(
        platform=platform,
        user_id=user_id,
        user_nickname=first.sender_name,
        user_cardname=first.sender_name if group else None,
    )
    if group is None:
        storage.remember(user_id, first.session_name, "private")
    bot = UserInfo(platform=platform, user_id=stable_id(platform, "bot", bot_name), user_nickname=bot_name)
    segments: list[Seg] = []
    for item in batch:
        prefix = f'{item.sender_name}在群"{item.session_name}"中说：' if group and group_batch else ""
        if item.part.caption:
            segments.append(Seg(type="text", data=f"{prefix}[图片描述] {item.part.caption}"))
            prefix = ""
        if item.part.type in {"image", "emoji"} and item.part.data:
            if prefix:
                segments.append(Seg(type="text", data=prefix.rstrip("：")))
            segments.append(Seg(type="image", data=item.part.data))
        elif item.part.data:
            segments.append(Seg(type="text", data=prefix + item.part.data))
    if not segments:
        raise ValueError("消息批次没有可发送内容")
    segment = segments[0] if len(segments) == 1 else Seg(type="seglist", data=segments)
    message_id = first.message_id if len(batch) == 1 else hashlib.sha256(
        (platform + "\x1fbatch\x1f" + "\x1f".join(item.message_id for item in batch)).encode("utf-8")
    ).hexdigest()
    info = BaseMessageInfo(
        platform=platform,
        message_id=message_id,
        time=max(item.timestamp for item in batch),
        group_info=group,
        user_info=user,
        format_info=FormatInfo(
            content_format=[part.type for part in segments],
            accept_format=["text", "image", "emoji", "seglist", "reply", "notify"],
        ),
        sender_info=SenderInfo(group_info=group, user_info=user),
        receiver_info=ReceiverInfo(group_info=group, user_info=None if group else bot),
        additional_config={"platform_io_target_user_id": group.group_id if group else user_id},
    )
    raw_text = "\n".join(str(part.data) for part in segments if part.type == "text") or None
    return MessageBase(message_info=info, message_segment=segment, raw_message=raw_text)


def resolve_reply_target(message: MessageBase, platform: str, storage: Storage) -> tuple[str, str] | None:
    info = message.message_info
    candidates: list[str] = []
    for container in (info.receiver_info, info.sender_info):
        if container and container.group_info and container.group_info.group_id:
            candidates.append(container.group_info.group_id)
        if container and container.user_info and container.user_info.user_id:
            candidates.append(container.user_info.user_id)
    if info.group_info and info.group_info.group_id:
        candidates.append(info.group_info.group_id)
    if info.user_info and info.user_info.user_id:
        candidates.append(info.user_info.user_id)
    config: Any = info.additional_config or {}
    target = config.get("platform_io_target_user_id") if isinstance(config, dict) else None
    if target:
        candidates.append(str(target))
    for identifier in candidates:
        mapped = storage.resolve(identifier)
        if mapped:
            return mapped
    return None
