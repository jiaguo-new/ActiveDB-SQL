"""OpenAI-compatible LLM client for NL2SQL experiments."""

from __future__ import annotations

import os
import time
from typing import Any

import requests


class LLMClient:
    """Minimal OpenAI-compatible chat completions client."""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
        api_key_env: str = "GLM_API_KEY",
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key or os.environ.get(api_key_env)
        if not self.api_key:
            raise RuntimeError(
                f"Missing API key: set {api_key_env} environment variable or pass api_key."
            )
        self.timeout = timeout
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        top_p: float = 1.0,
        max_tokens: int = 1024,
        **extra: Any,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            **extra,
        }
        start = time.time()
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self.headers,
            json=payload,
            timeout=self.timeout,
        )
        latency = time.time() - start
        response.raise_for_status()
        data = response.json()
        return {
            "response": data,
            "latency_seconds": round(latency, 3),
            "request": payload,
        }

    def extract_content(self, completion: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        data = completion["response"]
        usage = data.get("usage")
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("Empty choices in LLM response")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        return content, usage
