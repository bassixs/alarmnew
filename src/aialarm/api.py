"""FastAPI: health-check, метрики воронки и простой дашборд.

Запуск: python -m aialarm.cli api  (или uvicorn aialarm.api:app)
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from aialarm.config import get_settings
from aialarm.db import init_db
from aialarm.reporting import funnel

app = FastAPI(title="aialarm", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "project": get_settings().project.project_name}


@app.get("/stats")
def stats(days: int = 7) -> dict:
    return funnel(days)


@app.get("/", response_class=HTMLResponse)
def dashboard(days: int = 7) -> str:
    f = funnel(days)
    rows = "".join(
        f"<tr><td>{k}</td><td style='text-align:right'>{v}</td></tr>" for k, v in f.items()
    )
    return f"""<!doctype html><meta charset=utf-8>
    <title>aialarm dashboard</title>
    <body style="font-family:system-ui;max-width:640px;margin:40px auto">
    <h1>aialarm — воронка за {f['period_days']} дн.</h1>
    <table style="width:100%;border-collapse:collapse" border=1 cellpadding=8>{rows}</table>
    <p style="color:#888">seen → relevant → published; reject_rate/clean_pass_rate — для калибровки.</p>
    </body>"""
