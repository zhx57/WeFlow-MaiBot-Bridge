from __future__ import annotations

import logging

import requests

from .config import CaptionConfig
from .media import ValidatedImage


log = logging.getLogger(__name__)


class CaptionProvider:
    def __init__(self, config: CaptionConfig) -> None:
        self.config = config

    def caption(self, image: ValidatedImage) -> str | None:
        if self.config.provider == "off":
            return None
        try:
            if self.config.provider == "ollama":
                response = requests.post(
                    self.config.base_url.rstrip("/") + "/api/generate",
                    json={
                        "model": self.config.model,
                        "prompt": self.config.prompt,
                        "images": [image.base64],
                        "stream": False,
                    },
                    timeout=self.config.timeout,
                )
                response.raise_for_status()
                return str(response.json().get("response") or "").strip() or None
            response = requests.post(
                self.config.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json={
                    "model": self.config.model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.config.prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{image.mime};base64,{image.base64}"}},
                        ],
                    }],
                    "max_tokens": 300,
                },
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"]).strip() or None
        except (requests.RequestException, KeyError, TypeError, ValueError):
            log.warning("可选图片描述失败，仍将原图发送给 MaiBot", exc_info=True)
            return None
