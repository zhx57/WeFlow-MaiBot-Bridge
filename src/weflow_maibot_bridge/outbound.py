from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OutboundPart:
    kind: str
    data: Any


def parse_outbound_segments(segment: Any) -> list[OutboundPart]:
    result: list[OutboundPart] = []

    def visit(value: Any) -> None:
        seg_type = str(_get(value, "type", "")).strip().lower()
        data = _get(value, "data")
        if seg_type == "seglist":
            if not isinstance(data, (list, tuple)):
                raise ValueError("seglist.data 必须是数组")
            for child in data:
                visit(child)
            return
        if seg_type in {"reply", "notify"}:
            return
        if seg_type == "text":
            text = _text(data)
            if text:
                result.append(OutboundPart("text", text))
            return
        if seg_type == "image":
            sources = data if isinstance(data, (list, tuple)) else [data]
            if not sources or any(source is None for source in sources):
                raise ValueError("image 段缺少图片数据")
            result.extend(OutboundPart("image", source) for source in sources)
            return
        if seg_type == "emoji":
            if isinstance(data, str) and data.strip() and len(data.strip()) <= 64 and not data.strip().lower().startswith(("http://", "https://", "data:image/")):
                result.append(OutboundPart("text", data.strip()))
            else:
                result.append(OutboundPart("image", data))
            return
        raise ValueError(f"不支持的 MaiBot 消息段: {seg_type or '<empty>'}")

    visit(segment)
    return result


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return json.dumps(value, ensure_ascii=False, default=str).strip()
