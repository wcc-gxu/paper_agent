"""TaskLogger — 结构化 JSON 日志写入器。

每个子 Agent 入库任务独立的 .jsonl 文件:
  ~/.paper_search/logs/tasks/{task_id}.jsonl

7 种事件类型:
  task_start / stage_start / stage_progress / paper_progress /
  stage_done / task_done / task_error

paper_progress event_type 枚举:
  search_found / eval_complete /
  download_start|done|failed|skip /
  convert_start|done|failed|skip /
  index_start|done|failed / rank_done / survey_done

video_stage event_type 枚举 (VideoAgent):
  parse_link_done|failed / metadata_fetched|failed /
  video_download_done|failed / audio_extract_done|failed /
  transcribe_done|failed|skipped / summarize_done / analyze_done
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskLogger:
    """任务级 JSONL 日志写入器。

    用法:
        logger = TaskLogger(Path("~/.paper_search/logs/tasks"), "task-20260616-001")
        logger.task_start(task_id, project_id, plan)
        logger.stage_start(task_id, "search", 1, 7)
        logger.paper_progress(task_id, "download", "paper-1", "A Paper", "download_done")
        logger.task_done(task_id, {"total": 50})
    """

    def __init__(self, log_dir: Path, task_id: str):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.task_id = task_id
        self._path = self.log_dir / f"{task_id}.jsonl"

    def _write(self, event: dict):
        """追加一行 JSON 到日志文件。"""
        event.setdefault("ts", _now())
        event.setdefault("task_id", self.task_id)
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.error(f"Failed to write task log: {e}")

    # ── 7 种事件 ─────────────────────────────────────────

    def task_start(self, task_id: str, project_id: str, plan: dict = None):
        self._write({
            "event": "task_start",
            "task_id": task_id,
            "project_id": project_id,
            "plan": plan or {},
        })

    def stage_start(self, task_id: str, stage: str, stage_index: int, total_stages: int):
        self._write({
            "event": "stage_start",
            "task_id": task_id,
            "stage": stage,
            "stage_index": stage_index,
            "total_stages": total_stages,
        })

    def stage_progress(self, task_id: str, stage: str, current: int, total: int):
        self._write({
            "event": "stage_progress",
            "task_id": task_id,
            "stage": stage,
            "current": current,
            "total": total,
        })

    def paper_progress(self, task_id: str, stage: str, paper_id: str,
                       title: str, event_type: str):
        """单篇论文处理事件。

        event_type: search_found | eval_complete |
                    download_start|done|failed|skip |
                    convert_start|done|failed|skip |
                    index_start|done|failed |
                    rank_done | survey_done
        """
        self._write({
            "event": "paper_progress",
            "task_id": task_id,
            "stage": stage,
            "paper_id": paper_id,
            "title": title,
            "event_type": event_type,
        })

    def stage_done(self, task_id: str, stage: str, result: dict = None):
        self._write({
            "event": "stage_done",
            "task_id": task_id,
            "stage": stage,
            "result": result or {},
        })

    def task_done(self, task_id: str, result: dict = None):
        self._write({
            "event": "task_done",
            "task_id": task_id,
            "result": result or {},
        })

    def task_error(self, task_id: str, error: str, traceback: str = ""):
        self._write({
            "event": "task_error",
            "task_id": task_id,
            "error": error,
            "traceback": traceback[:2000] if traceback else "",
        })

    # ── 读取 ────────────────────────────────────────────

    def read_events(self) -> list[dict]:
        """读取所有日志事件。"""
        if not self._path.exists():
            return []
        events = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return events

    def get_progress(self) -> dict:
        """从日志中重建当前进度。

        Returns:
            {"task_id": str, "current_stage": str, "stage_index": int,
             "total_stages": int, "papers_processed": int, "papers_total": int,
             "events": list[str]}
        """
        events = self.read_events()
        current_stage = ""
        stage_index = 0
        total_stages = 0
        papers_processed = 0
        papers_total = 0
        recent_events = []

        for e in events:
            evt = e.get("event", "")
            if evt == "stage_start":
                current_stage = e.get("stage", "")
                stage_index = e.get("stage_index", 0)
                total_stages = e.get("total_stages", 0)
                recent_events.append(f"stage:{current_stage}")
            elif evt == "paper_progress":
                et = e.get("event_type", "")
                if et.endswith("_done"):
                    papers_processed += 1
                elif et == "search_found":
                    papers_total += 1
                recent_events.append(f"paper:{e.get('paper_id','')[:16]}:{et}")

        return {
            "task_id": self.task_id,
            "current_stage": current_stage,
            "stage_index": stage_index,
            "total_stages": total_stages,
            "papers_processed": papers_processed,
            "papers_total": papers_total,
            "recent_events": recent_events[-10:],
        }
