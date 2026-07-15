"""Celery 应用配置 — Redis broker + result backend.

启动 Worker:
    celery -A paper_search.agent.celery_app worker --loglevel=info --concurrency=4

启动 Beat:
    celery -A paper_search.agent.celery_app beat --loglevel=info
"""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

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

SUBSCRIPTION_CHECK_INTERVAL_MINUTES = int(
    os.getenv("SUBSCRIPTION_CHECK_INTERVAL_MINUTES", "60")
)

# ── Celery Beat: 定时任务 ──
# Phase 5: 把 v1 TimerEventSource 的两个系统 Timer 迁到这里
app.conf.beat_schedule = {
    "check-subscriptions": {
        "task": "paper_search.agent.celery_tasks.subscription_check_task",
        "schedule": crontab(minute=f"*/{max(SUBSCRIPTION_CHECK_INTERVAL_MINUTES, 1)}"),
        "options": {"queue": "default"},
    },
    # 系统健康检查（原 v1: TimerEventSource health_check 每 1200s）→ 每 20 分钟
    "health-check": {
        "task": "paper_search.agent.celery_tasks.health_check_task",
        "schedule": crontab(minute="*/20"),
        "options": {"queue": "default"},
    },
    # 日志清理（原 v1: TimerEventSource cleanup_logs 每 86400s）→ 每天 00:30
    "cleanup-logs": {
        "task": "paper_search.agent.celery_tasks.cleanup_logs_task",
        "schedule": crontab(hour=0, minute=30),
        "options": {"queue": "default"},
    },
    # Phase 4: 会话关闭检查 — 每 15 分钟扫描一次
    "session-close-check": {
        "task": "paper_search.agent.celery_tasks.session_close_check_task",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": "default"},
    },
}

# 自动发现 tasks
app.autodiscover_tasks(["paper_search.agent.celery_tasks"])
