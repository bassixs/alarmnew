"""Рендер подвала со ссылками под каждую площадку.

Подвал задаётся в config.publish.footers как строки из ссылок {text,url}. Здесь
превращаем структуру в готовый текст в нужном формате:
- html     -> для Telegram (parse_mode=HTML): <a href="url">text</a>
- markdown -> для MAX (format=markdown): [text](url)
"""
from __future__ import annotations

import html

from aialarm.config import get_settings


def render_footer(platform: str, fmt: str) -> str:
    rows = get_settings().project.publish.footers.get(platform, [])
    lines: list[str] = []
    for row in rows:
        parts = []
        for item in row:
            if item.url and fmt == "html":
                parts.append(f'<a href="{html.escape(item.url)}">{html.escape(item.text)}</a>')
            elif item.url and fmt == "markdown":
                parts.append(f"[{item.text}]({item.url})")
            else:
                parts.append(html.escape(item.text) if fmt == "html" else item.text)
        lines.append(" ".join(parts))
    return "\n".join(lines).strip()
