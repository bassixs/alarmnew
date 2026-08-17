"""Выбор и создание редакционных визуалов для главного городского канала."""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from aialarm.config import get_settings
from aialarm.llm.client import get_llm_client
from aialarm.logging import get_logger

log = get_logger(__name__)

GENERATED_IMAGES_DIR = Path("data/images/generated")
AI_LABEL = "Иллюстрация, созданная ИИ"

_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string", "enum": ["original", "generate", "none"]},
        "reason": {"type": "string"},
        "generation_brief": {"type": "string"},
    },
    "required": ["recommendation", "reason", "generation_brief"],
}

_AGENT_SYSTEM = """Ты — визуальный редактор городского новостного канала «Калуга, внимание».
Твоя задача — посоветовать модератору, какой визуал корректен для конкретной новости.

Выбери строго один вариант:
- original — исходное фото уместно и важно как свидетельство реального места, события,
  объекта или мероприятия;
- generate — лучше нейтральная редакционная иллюстрация; она не должна выдавать себя за
  документальное фото события;
- none — картинка сделает новость вводящей в заблуждение, слишком чувствительной или
  неуместной.

Правила:
- Если доступного исходного фото нет, никогда не выбирай original.
- Для ДТП, пожаров, преступлений, болезней, смертей, пострадавших и иных чувствительных
  тем предпочитай original, только если фото действительно документальное и разрешено.
  Генерация допустима лишь как очевидно условная редакционная иллюстрация без крови,
  реальных лиц, точного места или имитации документального кадра; иначе выбирай none.
- Генерация подходит для нейтральных городских, инфраструктурных, погодных, объясняющих,
  позитивных и бытовых материалов, если иллюстрация не будет выдана за факт.
- В reason дай одно короткое понятное объяснение для модератора.
- generation_brief: всегда дай 1–2 предложения на русском, пригодные для безопасной
  редакционной иллюстрации, даже когда рекомендуешь original. Только сюжет и предметы
  кадра, без стиля, текста, логотипов, фамилий и узнаваемых лиц.
Ответь строго JSON по схеме."""

_STYLE_PROMPT = """Создай квадратную 1:1 редакционную иллюстрацию для местного новостного
канала. Стиль: выразительная газетно-комиксная графика, чёрная тушь, крупные акценты
глубокого тёмно-красного, тёплая кремовая бумага, растровые точки, зерно, чуть грубая
печатная фактура, высокий контраст, толстая чёрная рамка, динамичная но ясная композиция.
Это именно редакционная иллюстрация, а не фоторепортаж и не доказательство события.
Никаких логотипов, брендов, водяных знаков, заголовков, реплик, читаемых вывесок,
номеров автомобилей, дат или другого текста. Не изображай узнаваемых реальных людей,
не показывай кровь, травмы, жестокость или катастрофу как якобы реальный кадр.
Внизу картинки должна быть очень маленькая неброская серая подпись на русском:
«Иллюстрация, созданная ИИ». Другого текста на изображении быть не должно.

Сюжет: {brief}"""


def recommend_visual(
    *,
    title: str,
    body: str,
    post_text: str,
    has_original: bool,
    is_sensitive: bool,
    visual_forbidden: bool,
) -> dict[str, str]:
    """Получить рекомендацию визуального агента и нормализовать его ответ."""
    llm = get_settings().project.llm
    user = (
        f"Заголовок: {title}\n"
        f"Исходный текст: {(body or '')[:2000]}\n"
        f"Готовый пост: {post_text[:2000]}\n"
        f"Есть разрешённое исходное фото: {'да' if has_original else 'нет'}\n"
        f"Чувствительная тема: {'да' if is_sensitive else 'нет'}\n"
        f"Визуал источника запрещён: {'да' if visual_forbidden else 'нет'}"
    )
    data = get_llm_client().structured(
        model=llm.visual_agent_model,
        system=_AGENT_SYSTEM,
        user=user,
        schema=_CHOICE_SCHEMA,
        max_tokens=450,
        temperature=0.1,
    )
    recommendation = str(data.get("recommendation", "none")).strip().lower()
    if recommendation not in {"original", "generate", "none"}:
        recommendation = "none"
    if recommendation == "original" and not has_original:
        recommendation = "none"
    return {
        "recommendation": recommendation,
        "reason": str(data.get("reason", "")).strip()[:500],
        "generation_brief": str(data.get("generation_brief", "")).strip()[:1500],
    }


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _add_ai_label(image_bytes: bytes) -> bytes:
    """Надёжно нанести маркировку поверх результата модели.

    Модель также получает это требование в промте, но мелкий кириллический текст она
    может исказить — серверная подпись гарантирует читабельность.
    """
    with Image.open(BytesIO(image_bytes)) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    font = _font(max(14, width // 48))
    padding = max(12, width // 50)
    bbox = draw.textbbox((0, 0), AI_LABEL, font=font)
    x = max(padding, width - padding - (bbox[2] - bbox[0]))
    y = max(padding, height - padding - (bbox[3] - bbox[1]))
    # Светлая подложка делает серую подпись читаемой на любом сюжете.
    draw.rounded_rectangle(
        (x - 7, y - 4, width - padding + 7, height - padding + 4),
        radius=4,
        fill=(235, 229, 214),
    )
    draw.text((x, y), AI_LABEL, font=font, fill=(110, 110, 110))
    output = BytesIO()
    image.save(output, format="JPEG", quality=94, optimize=True)
    return output.getvalue()


def generate_visual_file(brief: str) -> str:
    """Сгенерировать и сохранить квадратную иллюстрацию, вернуть локальный путь."""
    if not brief.strip():
        raise ValueError("Нет описания сюжета для генерации картинки")
    llm = get_settings().project.llm
    prompt = _STYLE_PROMPT.format(brief=brief.strip())
    data = get_llm_client().image(
        model=llm.image_model,
        prompt=prompt,
        size=llm.image_size,
    )
    labelled = _add_ai_label(base64.b64decode(data))
    GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    target = GENERATED_IMAGES_DIR / f"ai-{uuid4().hex}.jpg"
    target.write_bytes(labelled)
    log.info("visual_generated", path=str(target), bytes=len(labelled))
    return str(target)
