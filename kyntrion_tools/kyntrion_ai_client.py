"""Bounded and fail-closed client for the separately hosted consultation worker."""

from __future__ import annotations

import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from kyntrion_tools.kyntrion_marketing import SERVICES


class AIUnavailable(Exception):
    """The assistant cannot safely produce a response right now."""


class AIClient:
    def __init__(self, base_url: str, token: str, *, opener=None):
        if not isinstance(base_url, str):
            raise ValueError("Invalid consultation URL")
        target = urlsplit(base_url)
        if (target.scheme != "https" or not target.hostname or target.username
                or target.password or target.query or target.fragment
                or target.path not in ("", "/") or target.port is not None):
            raise ValueError("Consultation URL must be a secure HTTPS origin")
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError("Missing worker authorization")
        self.url = base_url.rstrip("/") + "/consult"
        self.token = token
        self.opener = opener or urlopen

    def reply(self, category: str, messages: list[dict]) -> str:
        if category not in SERVICES or not isinstance(messages, list) or not 1 <= len(messages) <= 12:
            raise AIUnavailable("Неверные параметры консультации")
        for item in messages:
            if (not isinstance(item, dict) or item.get("role") not in ("user", "assistant")
                    or not isinstance(item.get("content"), str)
                    or not 1 <= len(item["content"]) <= 2000):
                raise AIUnavailable("Неверный формат консультации")
        payload = json.dumps({"category": category, "messages": messages}, ensure_ascii=False).encode()
        request = Request(self.url, data=payload, headers={
            "Content-Type": "application/json", "X-Kyntrion-Bot-Token": self.token,
        }, method="POST")
        try:
            with self.opener(request, timeout=12) as response:
                data = json.loads(response.read(16000))
            result = data.get("result") if isinstance(data, dict) and data.get("ok") is True else None
            answer = result.get("text") if isinstance(result, dict) else None
            if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 1800:
                raise AIUnavailable("ИИ временно недоступен")
            return answer.strip()
        except AIUnavailable:
            raise
        except Exception as exc:
            # Never expose upstream errors: they may include request headers/secrets.
            raise AIUnavailable("ИИ временно недоступен") from None
