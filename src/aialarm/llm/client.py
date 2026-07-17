"""LLM-клиент через OpenAI-совместимый API (по умолчанию AiTunnel).

Почему OpenAI SDK, а не Anthropic: у пользователя ключ от AiTunnel — прокси-агрегатора
с OpenAI-совместимым форматом (`/v1/chat/completions`, `Authorization: Bearer`). Один и
тот же код работает с AiTunnel, обычным OpenAI и любым совместимым шлюзом — меняется лишь
base_url и строка model.

Structured output: не полагаемся на tool/function-calling, т.к. через прокси его
поддержка зависит от нижележащей модели. Вместо этого просим строгий JSON по схеме и
надёжно парсим (с попыткой response_format=json_object и fallback-извлечением скобок).
Это переносимо между claude-*, gpt-*, deepseek-* и т.п.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from aialarm.config import get_settings
from aialarm.logging import get_logger

log = get_logger(__name__)


def _extract_json(text: str) -> dict[str, Any]:
    """Распарсить JSON из ответа модели, срезая markdown-обёртку и лишний текст."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class LLMClient:
    def __init__(self, api_key: str, base_url: str):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def structured(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 1500,
        temperature: float = 0.0,
        tool_name: str = "result",  # совместимость с прежней сигнатурой; не используется
    ) -> dict[str, Any]:
        sys_prompt = (
            f"{system}\n\n"
            "Ответь СТРОГО одним JSON-объектом по схеме ниже, без markdown и пояснений:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            resp = self._client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
        except Exception as e:  # noqa: BLE001
            # Некоторые модели прокси не принимают response_format — повторяем без него.
            log.info("llm_no_json_mode", model=model, error=str(e)[:120])
            resp = self._client.chat.completions.create(**kwargs)

        content = resp.choices[0].message.content or ""
        if not content.strip():
            # Некоторые модели/прокси изредка возвращают пустой content при json-mode —
            # повторяем тем же запросом без response_format.
            log.info("llm_empty_content_retry", model=model)
            resp = self._client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
        try:
            return _extract_json(content)
        except json.JSONDecodeError as e:
            log.error("llm_bad_json", model=model, content=content[:500])
            raise RuntimeError("LLM не вернула валидный JSON") from e


@lru_cache(maxsize=1)
def get_llm_client() -> LLMClient:
    s = get_settings().secrets
    if not s.llm_api_key:
        raise RuntimeError("LLM_API_KEY не задан в .env")
    return LLMClient(s.llm_api_key, s.llm_base_url)
