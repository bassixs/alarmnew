"""Рендер подвала со ссылками под каждую площадку.

Подвал задаётся в config.publish.footers как строки из ссылок {text,url}. Здесь
превращаем структуру в готовый текст в нужном формате:
- html     -> для Telegram (parse_mode=HTML): <a href="url">text</a>
- markdown -> для MAX (format=markdown): [text](url)
"""
from __future__ import annotations

import html

from aialarm.config import FooterItem, get_settings


def render_footer_rows(rows: list[list[FooterItem]], fmt: str) -> str:
    """Отрисовать заранее выбранный набор строк подвала."""
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


def render_footer(platform: str, fmt: str) -> str:
    return render_footer_rows(get_settings().project.publish.footers.get(platform, []), fmt)


def render_source_link(source_url: str | None, fmt: str) -> str:
    """Короткая кликабельная строка об источнике для согласованных постов."""
    if not source_url:
        return ""
    if fmt == "html":
        return f'<a href="{html.escape(source_url, quote=True)}">Источник</a>'
    if fmt == "markdown":
        return f"[Источник]({source_url})"
    return "Источник"
