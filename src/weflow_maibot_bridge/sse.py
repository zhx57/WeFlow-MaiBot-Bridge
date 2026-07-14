from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str = "message"
    id: str | None = None
    retry: int | None = None


class SSEParser:
    def __init__(self) -> None:
        self._data: list[str] = []
        self._event = "message"
        self._id: str | None = None
        self._retry: int | None = None

    def feed(self, line: str | bytes) -> SSEEvent | None:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.rstrip("\r\n")
        if not line:
            if not self._data:
                self._event = "message"
                self._retry = None
                return None
            event = SSEEvent("\n".join(self._data), self._event, self._id, self._retry)
            self._data = []
            self._event = "message"
            self._retry = None
            return event
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value or "message"
        elif field == "id" and "\0" not in value:
            self._id = value
        elif field == "retry":
            try:
                retry = int(value)
                self._retry = retry if retry >= 0 else None
            except ValueError:
                pass
        return None


def parse_sse(lines: Iterable[str | bytes]) -> Iterator[SSEEvent]:
    parser = SSEParser()
    for line in lines:
        event = parser.feed(line)
        if event is not None:
            yield event
    event = parser.feed("")
    if event is not None:
        yield event
