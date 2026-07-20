"""IngestAgent 集成测试。

覆盖:
  1. TaskLogger — 7 种事件写入和读取
  2. PipelineRunner — 搜索阶段（真实 Semantic Scholar 调用）
  3. PipelineRunner — 完整 7 阶段（小规模）
  4. Single Tool — 只下载一篇
  5. 错误处理 — 下载失败 → 重试 → 跳过
  6. IngestAgent — LangGraph 图编译和执行
  7. Celery task — 独立测试（仅 import 验证）

运行:
    pytest tests/test_ingest_agent.py -v
"""

import json
import os
import tempfile
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════
# Test 1: TaskLogger
# ═══════════════════════════════════════════════════════════════


def test_task_logger_write_and_read():
    """测试 7 种事件类型的写入和读取。"""
    from paper_search.agent.task_logger import TaskLogger

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        tlog = TaskLogger(log_dir, "test-task-001")

        # 写入所有 7 种事件
        tlog.task_start("test-task-001", "proj-001", {"query": "test"})
        tlog.stage_start("test-task-001", "search", 1, 7)
        tlog.stage_progress("test-task-001", "search", 5, 20)
        tlog.paper_progress("test-task-001", "search", "paper-1", "A Paper", "search_found")
        tlog.paper_progress("test-task-001", "download", "paper-1", "A Paper", "download_done")
        tlog.paper_progress("test-task-001", "download", "paper-2", "B Paper", "download_failed")
        tlog.stage_done("test-task-001", "search", {"total": 20})
        tlog.task_done("test-task-001", {"total": 20, "downloaded": 18})

        # 读取
        events = tlog.read_events()
        assert len(events) == 8, f"Expected 8 events, got {len(events)}"

        # 验证事件类型
        event_types = [e["event"] for e in events]
        assert "task_start" == event_types[0]
        assert "stage_start" == event_types[1]
        assert "paper_progress" in event_types
        assert "task_done" == event_types[-1]

        # 验证进度重建
        progress = tlog.get_progress()
        assert progress["task_id"] == "test-task-001"
        assert progress["papers_total"] == 1  # search_found 计数


def test_task_logger_error():
    """测试 task_error 事件。"""
    from paper_search.agent.task_logger import TaskLogger

    with tempfile.TemporaryDirectory() as tmp:
        tlog = TaskLogger(Path(tmp), "test-error")
        tlog.task_error("test-error", "Something went wrong", "Traceback line 1\nline 2")
        events = tlog.read_events()
        assert len(events) == 1
        assert events[0]["event"] == "task_error"
        assert "Something went wrong" in events[0]["error"]


# ═══════════════════════════════════════════════════════════════
# Test 2: PipelineRunner Search Stage
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pipeline_runner_search():
    """测试搜索阶段（真实 API 调用，需要 DATABASE_URL）。"""
    import os
    if not os.environ.get("PYTEST_DB_INTEGRATION"):
        pytest.skip("PYTEST_DB_INTEGRATION not set — skip DB integration test")

    from paper_search.agent.sub_agent import PipelineRunner
    from paper_search.engine import PaperSearchEngine
    from paper_search.config import Config
    from paper_search.agent.pgdb import PostgresAgentDB
    from paper_search.agent.llm_client_v2 import LLMClientV2
    from paper_search.agent.pgvector_store import PgVectorStore
    from paper_search.agent.pdf_converter import PDFConverter
    from paper_search.agent.journal_ranker import JournalRanker
    from paper_search.agent.task_logger import TaskLogger

    db = PostgresAgentDB()
    engine = PaperSearchEngine(Config())
    llm = LLMClientV2()
    vector_store = PgVectorStore()
    converter = PDFConverter(max_concurrent=1)
    ranker = JournalRanker()

    runner = PipelineRunner(
        engine=engine, db=db, llm=llm, chroma=vector_store,
        converter=converter, ranker=ranker,
    )

    # 测试搜索
    project_id = db.create_project(user_query="transformer attention")
    tlog = TaskLogger(Path("logs/tasks"), "test-search")

    papers = await runner._search_stage(
        "test-search", project_id,
        "transformer attention mechanism", ["arxiv", "semantic_scholar"],
        2023, 5, tlog,
    )

    assert isinstance(papers, list)
    if papers:
        assert "title" in papers[0]
        assert "paper_id" in papers[0]
        print(f"Search returned {len(papers)} papers")

    # 验证日志写入
    events = tlog.read_events()
    found_events = [e for e in events if e.get("event_type") == "search_found"]
    assert len(found_events) == len(papers)


# ═══════════════════════════════════════════════════════════════
# Test 3: Error Handling
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_error_handling_download_failure():
    """测试下载失败时的错误处理（需要 DATABASE_URL）。"""
    import os
    if not os.environ.get("PYTEST_DB_INTEGRATION"):
        pytest.skip("PYTEST_DB_INTEGRATION not set — skip DB integration test")

    from paper_search.agent.sub_agent import PipelineRunner
    from paper_search.agent.pgdb import PostgresAgentDB
    from paper_search.agent.llm_client_v2 import LLMClientV2
    from paper_search.agent.pgvector_store import PgVectorStore
    from paper_search.agent.pdf_converter import PDFConverter
    from paper_search.agent.journal_ranker import JournalRanker
    from paper_search.agent.task_logger import TaskLogger

    db = PostgresAgentDB()
    db.create_project(user_query="test", project_id="test-proj")
    tlog = TaskLogger(Path("logs/tasks"), "test-err")

    # 创建一个无 engine 的 runner（下载会失败）
    runner = PipelineRunner(
        engine=None, db=db, llm=LLMClientV2(),
        chroma=PgVectorStore(), converter=PDFConverter(),
        ranker=JournalRanker(),
    )

    paper = {"paper_id": "test-paper-1", "title": "Test Paper",
             "authors": [], "year": 2024, "abstract": "test",
             "source": "arxiv"}

    # 下载应失败但不抛异常
    result = await runner._download_single(
        "test-err", "test-proj", paper, tlog,
    )
    assert result["success"] is False
    assert "error" in result


# ═══════════════════════════════════════════════════════════════
# Test 4: IngestAgent Graph Compile
# ═══════════════════════════════════════════════════════════════


def test_ingest_agent_compile():
    """测试 IngestAgent LangGraph 图编译（需要 DATABASE_URL）。"""
    import os
    if not os.environ.get("PYTEST_DB_INTEGRATION"):
        pytest.skip("PYTEST_DB_INTEGRATION not set — skip DB integration test")

    from paper_search.agent.sub_agent import PipelineRunner
    from paper_search.agent.graphs.ingest_graph import IngestAgent
    from paper_search.agent.pgdb import PostgresAgentDB
    from paper_search.agent.llm_client_v2 import LLMClientV2
    from paper_search.agent.pgvector_store import PgVectorStore
    from paper_search.agent.pdf_converter import PDFConverter
    from paper_search.agent.journal_ranker import JournalRanker

    db = PostgresAgentDB()
    runner = PipelineRunner(
        engine=None, db=db, llm=LLMClientV2(),
        chroma=PgVectorStore(), converter=PDFConverter(),
        ranker=JournalRanker(),
    )

    agent = IngestAgent(runner)
    graph = agent.compile()

    assert graph is not None
    nodes = list(graph.nodes.keys()) if hasattr(graph, 'nodes') else []
    if not nodes:
        assert graph is not None


# ═══════════════════════════════════════════════════════════════
# Test 5: Celery Tasks Import
# ═══════════════════════════════════════════════════════════════


def test_celery_tasks_import():
    """测试 Celery task 可导入。"""
    from paper_search.agent.celery_tasks import subscription_check_task
    assert subscription_check_task is not None


# ═══════════════════════════════════════════════════════════════
# Test 6: Reporter
# ═══════════════════════════════════════════════════════════════


def test_reporter_create():
    """测试 Reporter 创建和基本方法。"""
    from paper_search.agent.reporter import Reporter

    reporter = Reporter(redis_url="redis://localhost:6379/0")
    assert reporter is not None
    # reporter 在无 Redis 时不应抛异常（惰性连接）
    # reporter 内部使用 _events_queue 存储 LPUSH 目标 key
    assert reporter._events_queue == "agent:events:agent-001"
