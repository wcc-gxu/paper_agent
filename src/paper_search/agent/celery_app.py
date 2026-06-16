"""Celery 应用配置 — Redis broker + result backend.

启动 Worker:
    celery -A paper_search.agent.celery_app worker --loglevel=info --concurrency=4

启动 Beat:
    celery -A paper_search.agent.celery_app beat --loglevel=info
"""

from __future__ import annotations

import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")

app = Celery(
    "paper_search",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
)

app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_default_retry_delay=30,
    task_max_retries=1,
    broker_connection_retry_on_startup=True,
)

# 自动发现 tasks
app.autodiscover_tasks(["paper_search.agent.celery_tasks"])
