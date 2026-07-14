from __future__ import annotations

import base64
import binascii
import ipaddress
import mimetypes
import os
import socket
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests


MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    raw: bytes
    mime: str
    suffix: str

    @property
    def base64(self) -> str:
        return base64.b64encode(self.raw).decode("ascii")


def validate_image(raw: bytes, max_bytes: int, content_type: str | None = None) -> ValidatedImage:
    if not raw:
        raise ValueError("图片内容为空")
    if len(raw) > max_bytes:
        raise ValueError("图片超过尺寸上限")
    detected: tuple[str, str] | None = None
    for magic, mime, suffix in MAGIC:
        if raw.startswith(magic):
            detected = (mime, suffix)
            break
    if raw.startswith(b"RIFF") and len(raw) >= 12 and raw[8:12] == b"WEBP":
        detected = ("image/webp", ".webp")
    if detected is None:
        raise ValueError("图片魔数不受支持或内容伪造")
    header_mime = (content_type or "").split(";", 1)[0].strip().lower()
    if header_mime and (not header_mime.startswith("image/") or header_mime != detected[0]):
        raise ValueError(f"图片 MIME 与内容不一致: {header_mime}")
    return ValidatedImage(raw, *detected)


def decode_base64_image(value: str, max_bytes: int) -> ValidatedImage:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少 Base64 图片")
    value = value.strip()
    content_type = None
    if value.lower().startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ValueError("图片 data URI 必须使用 Base64")
        content_type = header[5:].split(";", 1)[0]
    compact = "".join(value.split())
    if len(compact) > ((max_bytes + 2) // 3) * 4 + 8:
        raise ValueError("Base64 图片超过尺寸上限")
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("无效 Base64 图片") from exc
    return validate_image(raw, max_bytes, content_type)


def validate_public_url(url: str, resolver: Callable[..., list] = socket.getaddrinfo) -> None:
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("图片 URL 无效或过长")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("图片 URL 仅支持 HTTP(S)")
    if parsed.username or parsed.password:
        raise ValueError("图片 URL 禁止包含用户凭据")
    try:
        addresses = resolver(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("图片 URL 域名解析失败") from exc
    if not addresses:
        raise ValueError("图片 URL 域名没有可用地址")
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("图片 URL 解析到非公网地址")


def read_local_image(path_value: str, max_bytes: int, roots: tuple[Path, ...]) -> ValidatedImage:
    path = Path(path_value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("本地图片路径不是文件")
    if not roots:
        raise ValueError("本地图片默认禁用，请配置 media.local_roots")
    if not any(path.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("本地图片路径不在允许目录内")
    if path.stat().st_size > max_bytes:
        raise ValueError("本地图片超过尺寸上限")
    return validate_image(path.read_bytes(), max_bytes, mimetypes.guess_type(path)[0])


class SafeImageDownloader:
    def __init__(self, max_bytes: int, timeout: float, max_redirects: int) -> None:
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_redirects = max_redirects

    def download(self, url: str) -> ValidatedImage:
        session = requests.Session()
        current = url
        for redirect in range(self.max_redirects + 1):
            validate_public_url(current)
            response = session.get(
                current,
                stream=True,
                allow_redirects=False,
                timeout=(min(self.timeout, 5.0), self.timeout),
                headers={"Accept": "image/png,image/jpeg,image/gif,image/webp", "User-Agent": "WeFlow-MaiBot-Bridge/0.1"},
            )
            if response.is_redirect:
                if redirect == self.max_redirects:
                    raise ValueError("图片重定向次数过多")
                current = urljoin(current, response.headers.get("Location", ""))
                response.close()
                continue
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length and int(length) > self.max_bytes:
                raise ValueError("远程图片超过尺寸上限")
            raw = bytearray()
            for chunk in response.iter_content(64 * 1024):
                raw.extend(chunk)
                if len(raw) > self.max_bytes:
                    raise ValueError("远程图片超过尺寸上限")
            return validate_image(bytes(raw), self.max_bytes, response.headers.get("Content-Type"))
        raise ValueError("图片下载失败")


def write_unique_image(image: ValidatedImage, directory: Path, prefix: str = "image") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}-{uuid.uuid4().hex}{image.suffix}"
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(image.raw)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path
