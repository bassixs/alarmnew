"""Публикация в MAX через HTTP Bot API.

Домен/заголовок — из config.max_platform (MAX мигрирует). chat_id — query-параметр,
тело — {text, format, attachments}. Кликабельный подвал добавляем в формате markdown.
Картинку сначала загружаем: POST /uploads?type=image -> upload_url -> POST файла ->
token/photos -> attachments:[{type:image, payload}]. Если фото не загрузилось —
публикуем текстом (пост не теряем).
"""
from __future__ import annotations

from pathlib import Path

import httpx

from aialarm.config import FooterItem, channels_for_profile, get_settings
from aialarm.control import get_publish_profile
from aialarm.logging import get_logger
from aialarm.media import MAX_IMAGES_PER_POST
from aialarm.publishers.base import Post, PublishResult
from aialarm.publishers.footer import render_footer, render_footer_rows, render_source_link

log = get_logger(__name__)

_TEXT_LIMIT = 4000


async def _load_bytes(ref: str) -> bytes | None:
    try:
        if not ref.startswith(("http://", "https://")):
            p = Path(ref)
            return p.read_bytes() if p.exists() else None
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(ref, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.content
    except Exception as e:  # noqa: BLE001
        log.warning("max_image_load_failed", ref=ref[:80], error=str(e))
        return None


class MaxPublisher:
    platform = "max"

    def __init__(
        self,
        profile: str | None = None,
        chat_id: str | None = None,
        footer_rows: list[list[FooterItem]] | None = None,
    ):
        s = get_settings()
        self._profile = profile or get_publish_profile()
        channels = channels_for_profile(self._profile)
        self._token = s.secrets.max_bot_token
        self._chat_id = chat_id or channels.max
        self._footer_rows = footer_rows
        self._base = s.project.max_platform.base_url.rstrip("/")
        self._auth = s.project.max_platform.auth_header

    def _headers(self) -> dict:
        return {self._auth: self._token}

    async def _upload_image(self, client: httpx.AsyncClient, img: bytes) -> dict | None:
        """Загрузить картинку в MAX, вернуть payload для attachments или None."""
        try:
            r1 = await client.post(
                f"{self._base}/uploads", params={"type": "image"}, headers=self._headers()
            )
            r1.raise_for_status()
            upload_url = r1.json().get("url")
            if not upload_url:
                return None
            r2 = await client.post(upload_url, files={"data": ("image.jpg", img, "image/jpeg")})
            r2.raise_for_status()
            data = r2.json()
            if data.get("photos"):
                return {"photos": data["photos"]}
            if data.get("token"):
                return {"token": data["token"]}
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("max_image_upload_failed", error=str(e))
            return None

    def _text(self, post: Post) -> str:
        body = post.rendered_text(_TEXT_LIMIT)
        footer = (
            render_footer_rows(self._footer_rows, "markdown")
            if self._footer_rows is not None
            else render_footer("max", "markdown")
        )
        source = render_source_link(post.source_url, "markdown")
        parts = [body, source, footer]
        return "\n\n".join(part for part in parts if part)

    async def publish(self, post: Post) -> PublishResult:
        if not self._token or not self._chat_id:
            return PublishResult(ok=False, error=f"MAX не настроен для профиля {self._profile}")

        body: dict = {"text": self._text(post), "format": "markdown"}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                attachments: list[dict] = []
                for ref in post.image_refs()[:MAX_IMAGES_PER_POST]:
                    image = await _load_bytes(ref)
                    if not image:
                        continue
                    payload = await self._upload_image(client, image)
                    if payload:
                        attachments.append({"type": "image", "payload": payload})
                if attachments:
                    body["attachments"] = attachments

                resp = await client.post(
                    f"{self._base}/messages",
                    params={"chat_id": str(self._chat_id)},
                    json=body,
                    headers={**self._headers(), "Content-Type": "application/json"},
                )
            if resp.status_code == 429:
                return PublishResult(ok=False, error="MAX rate limited", rate_limited=True)
            resp.raise_for_status()
            data = resp.json() if resp.content else {}
            msg = data.get("message", {}) if isinstance(data, dict) else {}
            ext_id = str(msg.get("body", {}).get("mid") or "")
            return PublishResult(ok=True, external_id=ext_id or None)
        except httpx.HTTPStatusError as e:
            b = e.response.text[:300] if e.response is not None else ""
            log.error("max_publish_failed", status=e.response.status_code, body=b)
            return PublishResult(ok=False, error=f"HTTP {e.response.status_code}: {b}")
        except Exception as e:  # noqa: BLE001
            log.error("max_publish_error", error=str(e))
            return PublishResult(ok=False, error=str(e))
