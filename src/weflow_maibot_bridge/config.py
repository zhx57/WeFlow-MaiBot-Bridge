from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WeFlowConfig:
    base_url: str = "http://127.0.0.1:5031"
    access_token: str = ""
    connect_timeout: float = 5.0
    read_timeout: float = 45.0
    retry_min_seconds: float = 1.0
    retry_max_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class MaiBotConfig:
    url: str = "ws://127.0.0.1:8000/ws"
    platform: str = "weflow"
    token: str = ""
    reconnect_max_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    bot_nicknames: tuple[str, ...] = ()
    bot_wxid: str = ""
    group_mode: Literal["mention", "all", "batch"] = "mention"
    debounce_seconds: float = 3.0
    queue_size: int = 1000
    media_concurrency: int = 4
    max_attempts: int = 10


@dataclass(frozen=True, slots=True)
class MediaConfig:
    directory: Path = Path("data/media")
    max_bytes: int = 10 * 1024 * 1024
    download_timeout: float = 20.0
    max_redirects: int = 3
    local_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptionConfig:
    provider: Literal["off", "ollama", "openai"] = "off"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    model: str = "llava"
    prompt: str = "请用中文简短描述这张图片。"
    timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class UIAConfig:
    dry_run: bool = False
    search_enabled: bool = True
    window_titles: tuple[str, ...] = ("微信", "WeChat")
    operation_timeout: float = 20.0


@dataclass(frozen=True, slots=True)
class StorageConfig:
    database: Path = Path("data/bridge.sqlite3")


@dataclass(frozen=True, slots=True)
class AppConfig:
    project_root: Path
    weflow: WeFlowConfig
    maibot: MaiBotConfig
    bridge: BridgeConfig
    media: MediaConfig
    caption: CaptionConfig
    uia: UIAConfig
    storage: StorageConfig


T = TypeVar("T")


def _section(cls: type[T], raw: dict[str, Any], name: str) -> T:
    allowed = {item.name for item in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"[{name}] 包含未知字段: {', '.join(sorted(unknown))}")
    try:
        return cls(**raw)
    except TypeError as exc:
        raise ConfigError(f"[{name}] 配置类型或字段错误: {exc}") from exc


def _url(value: str, name: str, schemes: set[str]) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or not parsed.hostname:
        raise ConfigError(f"{name} 必须是有效的 {sorted(schemes)} URL")


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML 解析失败: {exc}") from exc

    names = {"weflow", "maibot", "bridge", "media", "caption", "uia", "storage"}
    unknown = set(raw) - names
    if unknown:
        raise ConfigError(f"未知配置节: {', '.join(sorted(unknown))}")
    for name in names:
        if name in raw and not isinstance(raw[name], dict):
            raise ConfigError(f"[{name}] 必须是 TOML 表")

    root = config_path.parent
    weflow_raw = dict(raw.get("weflow", {}))
    env_token = os.environ.get("WEFLOW_ACCESS_TOKEN")
    if env_token is not None:
        weflow_raw["access_token"] = env_token
    weflow = _section(WeFlowConfig, weflow_raw, "weflow")
    maibot = _section(MaiBotConfig, dict(raw.get("maibot", {})), "maibot")
    bridge_raw = dict(raw.get("bridge", {}))
    if "bot_nicknames" in bridge_raw:
        bridge_raw["bot_nicknames"] = tuple(bridge_raw["bot_nicknames"])
    bridge = _section(BridgeConfig, bridge_raw, "bridge")
    media_raw = dict(raw.get("media", {}))
    media_raw["directory"] = _resolve(root, media_raw.get("directory", "data/media"))
    media_raw["local_roots"] = tuple(_resolve(root, item) for item in media_raw.get("local_roots", []))
    media = _section(MediaConfig, media_raw, "media")
    caption = _section(CaptionConfig, dict(raw.get("caption", {})), "caption")
    uia_raw = dict(raw.get("uia", {}))
    if "window_titles" in uia_raw:
        uia_raw["window_titles"] = tuple(uia_raw["window_titles"])
    uia = _section(UIAConfig, uia_raw, "uia")
    storage_raw = dict(raw.get("storage", {}))
    storage_raw["database"] = _resolve(root, storage_raw.get("database", "data/bridge.sqlite3"))
    storage = _section(StorageConfig, storage_raw, "storage")

    _validate(weflow, maibot, bridge, media, caption, uia)
    return AppConfig(root, weflow, maibot, bridge, media, caption, uia, storage)


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def _validate(
    weflow: WeFlowConfig,
    maibot: MaiBotConfig,
    bridge: BridgeConfig,
    media: MediaConfig,
    caption: CaptionConfig,
    uia: UIAConfig,
) -> None:
    _types(weflow, {
        "base_url": str, "access_token": str, "connect_timeout": (int, float),
        "read_timeout": (int, float), "retry_min_seconds": (int, float), "retry_max_seconds": (int, float),
    }, "weflow")
    _types(maibot, {
        "url": str, "platform": str, "token": str, "reconnect_max_seconds": (int, float),
    }, "maibot")
    _types(bridge, {
        "bot_nicknames": tuple, "bot_wxid": str, "group_mode": str,
        "debounce_seconds": (int, float), "queue_size": int, "media_concurrency": int, "max_attempts": int,
    }, "bridge")
    _types(media, {
        "directory": Path, "max_bytes": int, "download_timeout": (int, float),
        "max_redirects": int, "local_roots": tuple,
    }, "media")
    _types(caption, {
        "provider": str, "base_url": str, "api_key": str, "model": str,
        "prompt": str, "timeout": (int, float),
    }, "caption")
    _types(uia, {
        "dry_run": bool, "search_enabled": bool, "window_titles": tuple,
        "operation_timeout": (int, float),
    }, "uia")
    if not all(isinstance(item, str) and item.strip() for item in bridge.bot_nicknames):
        raise ConfigError("bridge.bot_nicknames 必须是非空字符串数组")
    if not all(isinstance(item, str) and item.strip() for item in uia.window_titles):
        raise ConfigError("uia.window_titles 必须是非空字符串数组")
    _url(weflow.base_url, "weflow.base_url", {"http", "https"})
    _url(maibot.url, "maibot.url", {"ws", "wss"})
    if not weflow.access_token:
        raise ConfigError("weflow.access_token 为空，可改用 WEFLOW_ACCESS_TOKEN")
    if not maibot.platform.strip():
        raise ConfigError("maibot.platform 不能为空")
    if bridge.group_mode not in {"mention", "all", "batch"}:
        raise ConfigError("bridge.group_mode 只能是 mention、all 或 batch")
    if not bridge.bot_nicknames and bridge.group_mode == "mention" and not bridge.bot_wxid:
        raise ConfigError("mention 模式至少配置 bot_nicknames 或 bot_wxid")
    for name, value in {
        "bridge.debounce_seconds": bridge.debounce_seconds,
        "bridge.queue_size": bridge.queue_size,
        "bridge.media_concurrency": bridge.media_concurrency,
        "bridge.max_attempts": bridge.max_attempts,
        "media.max_bytes": media.max_bytes,
        "uia.operation_timeout": uia.operation_timeout,
    }.items():
        if isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{name} 必须大于 0")
    if caption.provider not in {"off", "ollama", "openai"}:
        raise ConfigError("caption.provider 只能是 off、ollama 或 openai")
    if caption.provider == "openai" and not caption.base_url:
        raise ConfigError("openai caption 需要 caption.base_url")


def _types(instance: object, expected: dict[str, type | tuple[type, ...]], section: str) -> None:
    for name, accepted in expected.items():
        value = getattr(instance, name)
        # bool is an int subclass, but numeric timeout/count fields must not accept it.
        valid = isinstance(value, accepted) and not (
            isinstance(value, bool) and accepted is not bool
        )
        if not valid:
            raise ConfigError(f"{section}.{name} 类型错误")
