# aialarm

Сервис автопостинга региональных новостей в **Telegram** и **MAX**.
Конвейер: **сбор → фильтрация по тезисам → рерайт под стиль канала → модерация → публикация**.

## Архитектура

```
источники ──► collectors ──► raw_news ──► filtering ──► rewrite ──► moderation ──► publishers ──► TG / MAX
 (rss/tg/                    (dedup)     (rules→          (LLM)      (бот с          (адаптеры,
  scrape/agg)                            prefilter→                  кнопками)        лимиты)
                                          LLM)
```

Каждая стадия — отдельный модуль с функцией `run_*_stage()`. В пилоте их дёргает
`APScheduler` (`aialarm.pipeline.scheduler`), в проде те же функции становятся Celery-задачами.

| Слой | Модуль | Что делает |
|---|---|---|
| Сбор | `aialarm.collectors` | RSS/агрегаторы, Telegram (Telethon), скрапинг (robots.txt), дедуп |
| Фильтрация | `aialarm.filtering` | правила (exclude/sensitive) → эмбеддинг-префильтр → LLM-классификатор |
| LLM | `aialarm.llm` | клиент к OpenAI-совместимому шлюзу (AiTunnel), эмбеддинги с офлайн-fallback |
| Рерайт | `aialarm.rewrite` | переписывание под tone_of_voice, гарантированная атрибуция источника |
| Модерация | `aialarm.moderation` | маршрутизация + Telegram-бот с кнопками ✅/✏️/❌ |
| Публикация | `aialarm.publishers` | адаптеры `TelegramPublisher`/`MaxPublisher`, лимиты частоты |
| Мониторинг | `aialarm.reporting`, `aialarm.api` | воронка seen→relevant→published, дашборд, health |

Ключевые архитектурные решения и trade-off описаны в шапке каждого модуля.

## Быстрый старт (пилот)

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -e .                                    # + [embeddings] для качественного дедупа

cp .env.example .env            # заполнить ключи
cp config.example.yaml config.yaml   # заполнить каналы/источники/тезисы

python -m aialarm.cli initdb
python -m aialarm.cli run        # разовый проход: сбор → ... → публикация
python -m aialarm.cli scheduler  # постоянная работа по расписанию
python -m aialarm.cli bot        # в отдельном процессе — бот-модератор
python -m aialarm.cli api        # дашборд на http://127.0.0.1:8000
python -m aialarm.cli report --days 7
```

Чтение Telegram-каналов-источников требует одноразовой авторизации:
```bash
python scripts/telethon_login.py
```

## Конфигурация

- **Секреты** — только в `.env` (ключи, токены, DATABASE_URL).
- **Параметры** — в `config.yaml` (каналы, источники, тезисы, стоп-слова, тон, лимиты).
  См. подробные комментарии в `config.example.yaml`.

Пороги для калибровки первую неделю (ТЗ):
- `filter.llm_confidence_min` — порог публикации по уверенности LLM;
- `filter.embed_relevance_min` — жёсткость дешёвого префильтра;
- `filter.dedup_cosine_threshold` — чувствительность дедупа (> 0.85 = дубль).

## Модерация

`moderation.mode`:
- `all` — всё уходит на утверждение (рекомендуется первые 2–4 недели);
- `sensitive_only` — вручную только чувствительное/exclude, остальное автоматически;
- `off` — автопубликация (но чувствительное всё равно на модерацию — не роняем молча).

Узнать свой `admin_chat_id`: напишите боту `/start`.

## Публикация: особенности площадок

**Telegram** — Bot API, бот должен быть админом канала. Лимит подписи к фото 1024, текста 4096.

**MAX** (свериться с `dev.max.ru` перед запуском — API меняется):
- рабочий домен — `https://botapi.max.ru` (проверено 2026-07; `platform-api2.max.ru` не отвечает);
- авторизация заголовком `Authorization: <token>` (query-параметр `access_token` отключён);
- `chat_id` передаётся как **query-параметр** `POST /messages?chat_id=...`, тело — `{text, attachments}`;
- получить `chat_id` канала: `GET /chats` или `GET /updates` (бот должен быть админом канала);
- бота сначала добавить в канал, затем назначить админом;
- **картинки**: в пилоте MAX постит текстом. Для медиа нужен upload: `POST /uploads` → `token` →
  `attachments:[{type:image, payload:{token}}]` (TODO, передать URL напрямую нельзя);
- `base_url` и заголовок вынесены в `config.max_platform` (домены мигрировали — не хардкодить).

## Правовые заметки (проверить перед запуском)

- **Копирайт**: публикуем пересказ фактов своими словами (форма защищается, факт — нет),
  промт рерайта явно запрещает копировать структуру предложений; атрибуция источника обязательна.
- **Маркировка ИИ-контента**: сверьтесь с актуальными требованиями РФ на момент запуска
  и при необходимости заполните `publish.ai_disclosure` — законодательство меняется.
- Для официального/госканала — ручная модерация фактажа по каждому источнику минимум
  первые 2–4 недели (`moderation.mode: all`).

## Прод-профиль

```bash
pip install -e ".[prod,embeddings]"
# DATABASE_URL=postgresql+psycopg://...  (pgvector для дедупа)
# стадии -> Celery-задачи, расписание -> Celery beat (логика в aialarm.pipeline.runner без изменений)
```

## Тесты

```bash
pip install -e ".[dev]"
pytest            # офлайн-тесты (без сети/ключей): дедуп, правила, эмбеддинги, рендер поста
```
