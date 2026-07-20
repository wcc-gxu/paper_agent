"""Celery 异步任务 — 重量操作（download/convert/index/survey）。

每个 task 承担：
  1. 执行实际操作
  2. 更新 AgentDB 中的状态
  3. 通过 Reporter 向主 Agent 报告进度
  4. 失败时记录错误日志（重试 1 次）
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .celery_app import app

logger = logging.getLogger(__name__)


def _get_db():
    from .pgdb import PostgresAgentDB
    return PostgresAgentDB()


def _get_reporter():
    from .reporter import Reporter
    agent_id = os.getenv("AGENT_ID", "agent-001")
    return Reporter(os.getenv("REDIS_URL", "redis://localhost:6379/0"), agent_id=agent_id)


def _get_logger(task_id: str, agent_type: str = "ingest"):
    from .task_logger import TaskLogger
    log_dir = Path.home() / ".paper_search" / "logs" / "sub_agents" / agent_type
    return TaskLogger(log_dir, task_id)




# ═══════════════════════════════════════════════════════════════
# Feature: Daily Frontier Tracking (subscription check)
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=60)
def subscription_check_task(self) -> dict:
    """Celery Beat 定时任务: 检查所有启用订阅，发现新论文。

    由 Celery Beat 定时触发（默认每 60 分钟）。
    对每个订阅: 搜索 → 对比上次论文 ID → 新论文存入 subscriptions.results JSONB → Pub/Sub 推送。
    """
    import asyncio

    task_id = self.request.id
    task_logger = _get_logger(task_id, "subscription")
    reporter = _get_reporter()
    db = _get_db()

    try:
        subscriptions = db.list_subscriptions(enabled_only=True)
        if not subscriptions:
            return {"checked": 0, "new_papers": 0}

        total_new = 0
        from ..engine import PaperSearchEngine
        from ..config import Config
        from ..models import SearchQuery, SourceType

        engine = PaperSearchEngine(Config())
        loop = asyncio.new_event_loop()

        try:
            for sub in subscriptions:
                sub_id = sub["id"]
                sub_name = sub.get("name", sub_id)

                try:
                    keywords = sub.get("keywords", "")
                    sources = sub.get("sources", ["arxiv", "semantic_scholar"])
                    if isinstance(sources, str):
                        import json as _json
                        sources = _json.loads(sources)
                    last_paper_ids = set(sub.get("last_paper_ids", []))

                    stypes = [
                        SourceType(s) for s in sources
                        if s in [x.value for x in SourceType]
                    ]
                    if not stypes:
                        stypes = [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

                    query = SearchQuery(
                        keywords=keywords,
                        sources=stypes,
                        max_results=20,
                    )
                    result = loop.run_until_complete(engine.search(query))

                    # Detect new papers vs last_paper_ids
                    current_paper_ids = []
                    new_papers = []
                    for p in result.papers:
                        pid = db.upsert_paper(p)
                        current_paper_ids.append(pid)
                        if pid not in last_paper_ids:
                            paper_dict = {
                                "paper_id": pid,
                                "title": p.title,
                                "authors": p.authors[:5] if p.authors else [],
                                "year": p.year,
                                "abstract": (p.abstract or "")[:300],
                                "venue": p.venue or "",
                                "source": p.source.value if hasattr(p.source, "value") else str(p.source),
                                "doi": p.doi or "",
                            }
                            new_papers.append(paper_dict)

                    # Store results
                    for paper in new_papers:
                        db.save_subscription_result(sub_id, paper)

                    # Update subscription state
                    db.update_subscription(
                        sub_id,
                        last_checked_at=db._now(),
                        last_paper_ids=current_paper_ids,
                    )

                    if new_papers:
                        total_new += len(new_papers)
                        # Publish notification via Redis Pub/Sub → API process
                        reporter.publish_notification({
                            "subscription_id": sub_id,
                            "subscription_name": sub_name,
                            "new_papers": new_papers,
                        })
                        logger.info(
                            f"Subscription '{sub_name}': {len(new_papers)} new papers"
                        )

                except Exception as sub_err:
                    # Per-subscription isolation — one failure doesn't block others
                    logger.error(
                        f"Subscription '{sub_name}' check failed: {sub_err}",
                        exc_info=True,
                    )
                    continue

        finally:
            loop.close()

        reporter.report_done(task_id, {
            "subscriptions_checked": len(subscriptions),
            "new_papers_found": total_new,
        })
        return {
            "checked": len(subscriptions),
            "new_papers": total_new,
        }

    except Exception as e:
        error_str = str(e)
        logger.error(f"subscription_check_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        reporter.report_error(task_id, error_str)
        return {"checked": 0, "new_papers": 0, "error": error_str}
@app.task(bind=True, max_retries=1, default_retry_delay=120)
def literature_push_task(self) -> dict:
    """Celery Beat 每日任务: 基于用户订阅 + 论文库推断话题，推送最新论文。

    由 Celery Beat 每天触发一次。
    流程: 读取所有用户订阅 → 对每个订阅做语义搜索 → 过滤已入库 → 推送通知。
    """
    import asyncio

    task_id = self.request.id
    reporter = _get_reporter()
    db = _get_db()

    try:
        subscriptions = db.list_subscriptions(enabled_only=True)
        if not subscriptions:
            return {"checked": 0, "pushed": 0}

        total_pushed = 0
        engine = PaperSearchEngine(Config())
        loop = asyncio.new_event_loop()

        try:
            for sub in subscriptions:
                sub_id = sub["id"]
                sub_name = sub.get("name", sub_id)
                keywords = sub.get("keywords", "")
                if not keywords:
                    continue

                try:
                    import json as _json
                    sources = sub.get("sources", ["arxiv", "semantic_scholar"])
                    if isinstance(sources, str):
                        sources = _json.loads(sources)

                    stypes = [
                        SourceType(s) for s in sources
                        if s in [x.value for x in SourceType]
                    ] or [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR]

                    query = SearchQuery(
                        keywords=keywords,
                        sources=stypes,
                        max_results=10,
                        year_from=datetime.now().year,
                    )
                    result = loop.run_until_complete(engine.search(query))

                    pushed = 0
                    for paper in result.papers:
                        pid = db.upsert_paper(paper)
                        paper_dict = {
                            "paper_id": pid,
                            "title": paper.title,
                            "authors": paper.authors[:5] if paper.authors else [],
                            "year": paper.year,
                            "abstract": (paper.abstract or "")[:200],
                            "source": str(paper.source) if hasattr(paper, "source") else "",
                        }
                        db.save_subscription_result(sub_id, paper_dict)
                        pushed += 1

                    if pushed:
                        reporter.publish_notification({
                            "type": "literature_push",
                            "subscription_id": sub_id,
                            "subscription_name": sub_name,
                            "new_papers_count": pushed,
                            "message": f"「{sub_name}」发现 {pushed} 篇新论文",
                        })
                        total_pushed += pushed

                except Exception as sub_err:
                    logger.error(f"literature_push '{sub_name}' failed: {sub_err}")
                    continue

        finally:
            loop.close()

        reporter.report_done(task_id, {
            "subscriptions_checked": len(subscriptions),
            "papers_pushed": total_pushed,
        })
        return {"checked": len(subscriptions), "pushed": total_pushed}

    except Exception as e:
        error_str = str(e)
        logger.error(f"literature_push_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        reporter.report_error(task_id, error_str)
        return {"checked": 0, "pushed": 0, "error": error_str}



# ═══════════════════════════════════════════════════════════════
# Phase 5: System Timers migrated from v1 TimerEventSource
# ═══════════════════════════════════════════════════════════════


@app.task(bind=True, max_retries=1, default_retry_delay=30)
def health_check_task(self) -> dict:
    """系统健康检查（原 v1 TimerEventSource health_check）。

    每 20 分钟运行，检查:
      - SQLite 可读写
      - Redis 连通性
      - 磁盘空间
    失败时打印 warning，不抛异常（避免 Celery 反复重试）。
    """
    import shutil
    result = {"sqlite": False, "redis": False, "disk_free_gb": 0.0}
    # SQLite
    try:
        db = _get_db()
        db.conn.execute("SELECT 1").fetchone()
        result["sqlite"] = True
    except Exception as e:
        logger.warning(f"[health_check] SQLite failed: {e}")

    # Redis
    try:
        import redis as _redis
        r = _redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                            decode_responses=True)
        r.ping()
        result["redis"] = True
    except Exception as e:
        logger.warning(f"[health_check] Redis failed: {e}")

    # Disk
    try:
        from ..config import get_data_dir
        usage = shutil.disk_usage(get_data_dir())
        result["disk_free_gb"] = round(usage.free / (1024 ** 3), 2)
        if result["disk_free_gb"] < 1.0:
            logger.warning(f"[health_check] Low disk: {result['disk_free_gb']} GB free")
    except Exception as e:
        logger.warning(f"[health_check] Disk check failed: {e}")

    if not (result["sqlite"] and result["redis"]):
        logger.warning(f"[health_check] FAILED: {result}")
    else:
        logger.info(f"[health_check] OK: {result}")
    return result


@app.task(bind=True, max_retries=1, default_retry_delay=60)
def cleanup_logs_task(self) -> dict:
    """日志清理（原 v1 TimerEventSource cleanup_logs）。

    每天 00:30 运行。清理 ~/.paper_search/logs/ 下:
      - 30 天前的 agent.log.* 滚动归档
      - 30 天前的 sub_agents/*/*.jsonl
    """
    import time
    from pathlib import Path
    log_dir = Path.home() / ".paper_search" / "logs"
    if not log_dir.exists():
        return {"removed": 0, "skipped": "log dir not found"}

    cutoff = time.time() - (30 * 86400)  # 30 days
    removed = 0
    for f in log_dir.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                # 不清理当前活跃的 agent.log
                if f.name == "agent.log":
                    continue
                f.unlink()
                removed += 1
        except Exception as e:
            logger.debug(f"[cleanup_logs] skip {f}: {e}")
    logger.info(f"[cleanup_logs] removed {removed} old log files")
    return {"removed": removed}


# ── Phase 4: Session Close —————————————————————————————————


@app.task(bind=True, max_retries=1, default_retry_delay=60)
def session_close_check_task(self) -> dict:
    """Celery Beat 定时任务: 扫描并关闭过期会话。

    增量扫描策略:
      1. 读取 session_scan_markers 中的扫描水位线
      2. 扫描 updated_at > 水位线 且 updated_at < 1 小时前的活跃会话
      3. 将符合条件的会话 status 切换为 'closed'
      4. 更新水位线

    Returns:
        {"scanned": int, "closed": int, "error": str}
    """
    import json as _json

    task_id = self.request.id or "unknown"
    try:
        from .pgdb import PostgresAgentDB
        db = PostgresAgentDB()
    except Exception as e:
        logger.error(f"session_close_check_task: DB init failed: {e}")
        return {"scanned": 0, "closed": 0, "error": str(e)}

    reporter = _get_reporter()

    try:
        # 1) 读水位线
        marker_row = db.conn.execute(
            "SELECT last_scan_value -- FROM session_scan_markers (removed, use Redis) "
            "WHERE marker_type = %s",
            ("session_close_last_scan",),
        ).fetchone()
        last_scan = (
            marker_row["last_scan_value"]
            if marker_row
            else "2020-01-01T00:00:00Z"
        )

        now_iso = _now_iso()

        # 2) 扫描 1 小时前有过活动但现在无连接的会话
        active_sessions = db.conn.execute(
            """SELECT id, user_id, title, updated_at
               FROM sessions
               WHERE status = 'active'
                 AND updated_at > %s::timestamptz
                 AND updated_at < NOW() - INTERVAL '1 hour'
               ORDER BY updated_at ASC""",
            (last_scan,),
        ).fetchall()

        closed_count = 0
        skipped_count = 0

        for sess in active_sessions:
            sess_id = sess["id"]
            try:
                db.conn.execute(
                    "UPDATE sessions SET status = %s, updated_at = %s WHERE id = %s",
                    ("closed", now_iso, sess_id),
                )
                db.conn.commit()
                closed_count += 1
                logger.info(
                    "Session closed: id=%s title=%s",
                    sess_id, sess.get("title", ""),
                )
            except Exception as e:
                skipped_count += 1
                logger.warning(
                    "Failed to close session %s: %s", sess_id, e,
                )
                try:
                    db.conn.rollback()
                except Exception:
                    pass

        # 3) 更新水位线
        db.conn.execute(
            """-- UPDATE session_scan_markers (removed)
               SET last_scan_value = %s, updated_at = %s
               WHERE marker_type = %s""",
            (now_iso, now_iso, "session_close_last_scan"),
        )
        db.conn.commit()

        reporter.report_done(task_id, {
            "scanned": len(active_sessions),
            "closed": closed_count,
            "skipped": skipped_count,
        })
        return {
            "scanned": len(active_sessions),
            "closed": closed_count,
            "skipped": skipped_count,
        }

    except Exception as e:
        error_str = str(e)
        logger.error(f"session_close_check_task failed: {error_str}")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        reporter.report_error(task_id, error_str)
        return {"scanned": 0, "closed": 0, "error": error_str}


def _now_iso() -> str:
    """返回当前 UTC ISO 时间字符串."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
