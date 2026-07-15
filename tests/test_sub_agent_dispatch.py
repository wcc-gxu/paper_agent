"""集成测试: _run_graph_agent 对 5 个 graph 的依赖注入和分发签名验证。

目标:
  对 clustering / citation_chase / translation / video / rad_query 五种 agent_type
  - 验证 _run_graph_agent_async 正确按 agent_type 分发（不会跑成 ingest）
  - 验证构造函数收到的依赖参数与各 Agent 类的 __init__ 签名匹配
  - 验证传给 graph.ainvoke(state) 的字段与各 State TypedDict 匹配
  - 验证 reporter.publish_lifecycle 收到正确的 agent_type 和 agent_started/done

同时验证:
  - agent_type == "ingest" 仍走原 sub_agent_task 路径（不进 _run_graph_agent）

跑法:
    PYTHONPATH=src pytest tests/test_sub_agent_dispatch.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ════════════════════════════════════════════════════════════════
# 共用 fixture / helper
# ════════════════════════════════════════════════════════════════


def _make_reporter():
    """伪 reporter — 记录所有调用以便断言。"""
    rep = MagicMock()
    rep.publish_lifecycle = MagicMock()
    rep.publish_report = MagicMock()
    rep.report_done = MagicMock()
    rep.report_error = MagicMock()
    return rep


def _make_task_logger():
    """伪 task_logger — 全部 no-op 的 MagicMock。"""
    return MagicMock()


def _make_graph_ainvoke(result_dict):
    """构造一个 compile() → graph_obj → ainvoke 链。"""
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value=result_dict)
    return graph


@pytest.fixture
def common_mocks():
    """所有 _run_graph_agent_async 测试共享: db / llm / reporter / logger。

    同时重置 LLMClientV2 单例缓存 (_llm_client)，确保 patch LLMClientV2
    在每次测试中都能生效（不被已缓存的实例干扰）。
    """
    import paper_search.agent.llm_client_v2 as _llm_mod
    _cached = getattr(_llm_mod, "_llm_client", None)
    _llm_mod._llm_client = None

    db = MagicMock(name="AgentDB")
    llm = MagicMock(name="LLMClientV2")
    reporter = _make_reporter()
    task_logger = _make_task_logger()

    yield {
        "db": db,
        "llm": llm,
        "reporter": reporter,
        "task_logger": task_logger,
    }

    # 恢复单例（避免影响其他可能依赖它的模块）
    _llm_mod._llm_client = _cached


# ════════════════════════════════════════════════════════════════
# Test 1: clustering 分发
# ════════════════════════════════════════════════════════════════


class TestClusteringDispatch:
    """agent_type=clustering → ClusteringAgent(db, chroma, llm, on_progress)
       state={project_id, n_clusters}"""

    def test_clustering_dispatch_constructs_correctly(self, common_mocks):
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_chroma = MagicMock(name="ChromaStoreV2")
        mock_agent_inst = MagicMock(name="ClusteringAgent_inst")
        mock_agent_inst.compile.return_value = _make_graph_ainvoke(
            {"result": {"clusters": 3}, "n_clusters": 3}
        )

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.agent.pgvector_store.PgVectorStore",
                   return_value=mock_chroma), \
             patch("paper_search.agent.graphs.clustering_graph.ClusteringAgent",
                   return_value=mock_agent_inst) as mock_agent_cls:

            arguments = {"project_id": "proj-42", "n_clusters": 5}
            result = asyncio.run(_run_graph_agent_async(
                agent_type="clustering",
                arguments=arguments,
                log_id="log-001",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-42",
            ))

        # ── 1. ClusteringAgent 构造参数 ──
        mock_agent_cls.assert_called_once()
        call_args = mock_agent_cls.call_args
        # 位置参数: (db, chroma, llm)
        assert call_args.args[0] is common_mocks["db"]
        assert call_args.args[1] is mock_chroma
        assert call_args.args[2] is common_mocks["llm"]
        # on_progress 关键字
        assert "on_progress" in call_args.kwargs
        assert callable(call_args.kwargs["on_progress"])

        # ── 2. graph.ainvoke 入参 state ──
        graph = mock_agent_inst.compile.return_value
        graph.ainvoke.assert_awaited_once()
        state = graph.ainvoke.await_args.args[0]
        assert state == {"project_id": "proj-42", "n_clusters": 5}

        # ── 3. 返回值结构 ──
        assert result == {"clusters": 3}

    def test_clustering_uses_project_id_fallback(self, common_mocks):
        """arguments 没 project_id 时回退到 _run_graph_agent_async 的 project_id 形参。"""
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_chroma = MagicMock()
        mock_agent_inst = MagicMock()
        mock_agent_inst.compile.return_value = _make_graph_ainvoke({"result": {}})

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.agent.pgvector_store.PgVectorStore",
                   return_value=mock_chroma), \
             patch("paper_search.agent.graphs.clustering_graph.ClusteringAgent",
                   return_value=mock_agent_inst):

            asyncio.run(_run_graph_agent_async(
                agent_type="clustering",
                arguments={},  # 空 arguments
                log_id="log-002",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-fallback",
            ))

        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state["project_id"] == "proj-fallback"
        assert state["n_clusters"] == 0  # 默认值


# ════════════════════════════════════════════════════════════════
# Test 2: citation_chase 分发
# ════════════════════════════════════════════════════════════════


class TestCitationChaseDispatch:
    """agent_type=citation_chase → CitationChaseAgent(db, llm, engine, on_progress=...)
       state={project_id, seed_title, seed_doi, max_depth, direction}"""

    def test_citation_chase_dispatch_with_seed_title(self, common_mocks):
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_engine = MagicMock(name="PaperSearchEngine")
        mock_agent_inst = MagicMock(name="CitationChaseAgent_inst")
        mock_agent_inst.compile.return_value = _make_graph_ainvoke(
            {"result": {"layers": 2, "ingested": 8}}
        )

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.engine.PaperSearchEngine",
                   return_value=mock_engine), \
             patch("paper_search.agent.graphs.citation_chase_graph.CitationChaseAgent",
                   return_value=mock_agent_inst) as mock_agent_cls:

            arguments = {
                "project_id": "proj-cite",
                "seed_title": "Attention Is All You Need",
                "depth": 3,
                "direction": "backward",
            }
            result = asyncio.run(_run_graph_agent_async(
                agent_type="citation_chase",
                arguments=arguments,
                log_id="log-003",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-cite",
            ))

        # 构造: (db, llm, engine) + on_progress
        call_args = mock_agent_cls.call_args
        assert call_args.args[0] is common_mocks["db"]
        assert call_args.args[1] is common_mocks["llm"]
        assert call_args.args[2] is mock_engine
        assert "on_progress" in call_args.kwargs

        # state
        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state["project_id"] == "proj-cite"
        assert state["seed_title"] == "Attention Is All You Need"
        assert state["seed_doi"] == ""
        assert state["max_depth"] == 3  # 来自 depth 别名
        assert state["direction"] == "backward"

        assert result == {"layers": 2, "ingested": 8}

    def test_citation_chase_seed_paper_fallback_alias(self, common_mocks):
        """没 seed_title 时，应使用 seed_paper / query 别名填 seed_title。"""
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_engine = MagicMock()
        mock_agent_inst = MagicMock()
        mock_agent_inst.compile.return_value = _make_graph_ainvoke({"result": {}})

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.engine.PaperSearchEngine",
                   return_value=mock_engine), \
             patch("paper_search.agent.graphs.citation_chase_graph.CitationChaseAgent",
                   return_value=mock_agent_inst):

            asyncio.run(_run_graph_agent_async(
                agent_type="citation_chase",
                arguments={"seed_paper": "BERT paper"},
                log_id="log-004",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-x",
            ))

        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state["seed_title"] == "BERT paper"
        assert state["max_depth"] == 2  # 默认值
        assert state["direction"] == "both"  # 默认


# ════════════════════════════════════════════════════════════════
# Test 3: translation 分发
# ════════════════════════════════════════════════════════════════


class TestTranslationDispatch:
    """agent_type=translation → TranslationAgent(db, llm, chroma, on_progress)
       state={action, text, direction, project_id}"""

    def test_translation_dispatch(self, common_mocks):
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_chroma = MagicMock(name="ChromaStoreV2")
        mock_agent_inst = MagicMock(name="TranslationAgent_inst")
        mock_agent_inst.compile.return_value = _make_graph_ainvoke(
            {"result": {"translation": "Convolutional Neural Network"}}
        )

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.agent.pgvector_store.PgVectorStore",
                   return_value=mock_chroma), \
             patch("paper_search.agent.graphs.translation_graph.TranslationAgent",
                   return_value=mock_agent_inst) as mock_agent_cls:

            arguments = {
                "action": "translate_query",
                "text": "卷积神经网络",
                "direction": "zh2en",
                "project_id": "proj-tr",
            }
            result = asyncio.run(_run_graph_agent_async(
                agent_type="translation",
                arguments=arguments,
                log_id="log-005",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-tr",
            ))

        # 构造: (db, llm, chroma) + on_progress
        call_args = mock_agent_cls.call_args
        assert call_args.args[0] is common_mocks["db"]
        assert call_args.args[1] is common_mocks["llm"]
        assert call_args.args[2] is mock_chroma
        assert "on_progress" in call_args.kwargs

        # state
        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state == {
            "action": "translate_query",
            "text": "卷积神经网络",
            "direction": "zh2en",
            "project_id": "proj-tr",
        }
        assert result == {"translation": "Convolutional Neural Network"}

    def test_translation_query_alias_fills_text(self, common_mocks):
        """没 text 字段时，query 别名作为 text 输入。"""
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_chroma = MagicMock()
        mock_agent_inst = MagicMock()
        mock_agent_inst.compile.return_value = _make_graph_ainvoke({"result": {}})

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.agent.pgvector_store.PgVectorStore",
                   return_value=mock_chroma), \
             patch("paper_search.agent.graphs.translation_graph.TranslationAgent",
                   return_value=mock_agent_inst):

            asyncio.run(_run_graph_agent_async(
                agent_type="translation",
                arguments={"query": "attention"},
                log_id="log-006",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-y",
            ))

        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state["text"] == "attention"
        assert state["action"] == "translate_query"  # 默认


# ════════════════════════════════════════════════════════════════
# Test 4: video 分发
# ════════════════════════════════════════════════════════════════


class TestVideoDispatch:
    """agent_type=video → VideoAgent(downloader, whisper, llm, db, videos_dir, on_progress)
       state={project_id, user_query}"""

    def test_video_dispatch(self, common_mocks, tmp_path):
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_downloader = MagicMock(name="VideoDownloader")
        mock_whisper = MagicMock(name="WhisperModel")
        mock_agent_inst = MagicMock(name="VideoAgent_inst")
        mock_agent_inst.compile.return_value = _make_graph_ainvoke(
            {"result": {"url": "https://example.com/video/1", "title": "Demo"}}
        )

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.agent.video_downloader.VideoDownloader",
                   return_value=mock_downloader) as mock_dl_cls, \
             patch("paper_search.agent.celery_tasks._get_whisper_model",
                   return_value=mock_whisper), \
             patch("paper_search.config.get_videos_dir",
                   return_value=tmp_path), \
             patch("paper_search.agent.graphs.video_graph.VideoAgent",
                   return_value=mock_agent_inst) as mock_agent_cls:

            arguments = {
                "project_id": "proj-video",
                "query": "https://v.douyin.com/xxx 分析这条视频",
            }
            result = asyncio.run(_run_graph_agent_async(
                agent_type="video",
                arguments=arguments,
                log_id="log-007",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-video",
            ))

        # VideoDownloader 应被构造 (output_dir=videos_dir)
        mock_dl_cls.assert_called_once()
        # VideoAgent: 位置 (downloader, whisper, llm, db, videos_dir) + on_progress
        call_args = mock_agent_cls.call_args
        assert call_args.args[0] is mock_downloader
        assert call_args.args[1] is mock_whisper
        assert call_args.args[2] is common_mocks["llm"]
        assert call_args.args[3] is common_mocks["db"]
        assert call_args.args[4] == tmp_path
        assert "on_progress" in call_args.kwargs

        # state: project_id + user_query (来自 query 别名)
        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state["project_id"] == "proj-video"
        assert state["user_query"] == "https://v.douyin.com/xxx 分析这条视频"

        assert result == {"url": "https://example.com/video/1", "title": "Demo"}

    def test_video_handles_whisper_unavailable(self, common_mocks, tmp_path):
        """whisper 加载失败返回 None，VideoAgent 仍能构造（whisper=None）。"""
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_downloader = MagicMock()
        mock_agent_inst = MagicMock()
        mock_agent_inst.compile.return_value = _make_graph_ainvoke({"result": {}})

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.agent.video_downloader.VideoDownloader",
                   return_value=mock_downloader), \
             patch("paper_search.agent.celery_tasks._get_whisper_model",
                   return_value=None), \
             patch("paper_search.config.get_videos_dir",
                   return_value=tmp_path), \
             patch("paper_search.agent.graphs.video_graph.VideoAgent",
                   return_value=mock_agent_inst) as mock_agent_cls:

            asyncio.run(_run_graph_agent_async(
                agent_type="video",
                arguments={"url": "https://example.com/v"},
                log_id="log-008",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-z",
            ))

        # 第二个位置参数 whisper 应该是 None
        assert mock_agent_cls.call_args.args[1] is None
        # state.user_query 应该来自 url 别名
        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state["user_query"] == "https://example.com/v"


# ════════════════════════════════════════════════════════════════
# Test 5: rad_query 分发
# ════════════════════════════════════════════════════════════════


class TestRADQueryDispatch:
    """agent_type=rad_query → RADQueryAgent(knowledge_base, on_progress)
       state={question, project_id}"""

    def test_rad_query_dispatch(self, common_mocks):
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_chroma = MagicMock(name="ChromaStoreV2")
        mock_kb = MagicMock(name="KnowledgeBase")
        mock_agent_inst = MagicMock(name="RADQueryAgent_inst")
        mock_agent_inst.compile.return_value = _make_graph_ainvoke(
            {"result": {"answer": "RAG works.", "sources": []}}
        )

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.agent.pgvector_store.PgVectorStore",
                   return_value=mock_chroma), \
             patch("paper_search.agent.knowledge.KnowledgeBase",
                   return_value=mock_kb) as mock_kb_cls, \
             patch("paper_search.agent.graphs.rad_query_graph.RADQueryAgent",
                   return_value=mock_agent_inst) as mock_agent_cls:

            arguments = {
                "question": "What is the role of attention?",
                "project_id": "proj-rad",
            }
            result = asyncio.run(_run_graph_agent_async(
                agent_type="rad_query",
                arguments=arguments,
                log_id="log-009",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-rad",
            ))

        # KnowledgeBase(db, chroma, llm) 应被构造
        mock_kb_cls.assert_called_once_with(
            common_mocks["db"], mock_chroma, common_mocks["llm"]
        )
        # RADQueryAgent: (kb,) + on_progress
        call_args = mock_agent_cls.call_args
        assert call_args.args[0] is mock_kb
        assert "on_progress" in call_args.kwargs

        # state
        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state == {
            "question": "What is the role of attention?",
            "project_id": "proj-rad",
        }
        assert result == {"answer": "RAG works.", "sources": []}

    def test_rad_query_uses_query_alias(self, common_mocks):
        """没 question 字段时使用 query 别名。"""
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        mock_chroma = MagicMock()
        mock_kb = MagicMock()
        mock_agent_inst = MagicMock()
        mock_agent_inst.compile.return_value = _make_graph_ainvoke({"result": {}})

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]), \
             patch("paper_search.agent.pgvector_store.PgVectorStore",
                   return_value=mock_chroma), \
             patch("paper_search.agent.knowledge.KnowledgeBase",
                   return_value=mock_kb), \
             patch("paper_search.agent.graphs.rad_query_graph.RADQueryAgent",
                   return_value=mock_agent_inst):

            asyncio.run(_run_graph_agent_async(
                agent_type="rad_query",
                arguments={"query": "How does BPE work?"},
                log_id="log-010",
                reporter=common_mocks["reporter"],
                task_logger=common_mocks["task_logger"],
                project_id="proj-w",
            ))

        state = mock_agent_inst.compile.return_value.ainvoke.await_args.args[0]
        assert state["question"] == "How does BPE work?"
        assert state["project_id"] == "proj-w"


# ════════════════════════════════════════════════════════════════
# Test 6: 未知 agent_type 兜底 → 抛 ValueError
# ════════════════════════════════════════════════════════════════


class TestUnknownAgentType:
    """unsupported agent_type → ValueError（_handle_sub_agent 上游已校验，
    这里是双保险）。"""

    def test_unknown_agent_type_raises_value_error(self, common_mocks):
        import asyncio
        from paper_search.agent.celery_tasks import _run_graph_agent_async

        with patch("paper_search.agent.celery_tasks._get_db",
                   return_value=common_mocks["db"]), \
             patch("paper_search.agent.llm_client_v2.LLMClientV2",
                   return_value=common_mocks["llm"]):
            with pytest.raises(ValueError, match="Unsupported agent_type"):
                asyncio.run(_run_graph_agent_async(
                    agent_type="nonsense_type",
                    arguments={},
                    log_id="log-bad",
                    reporter=common_mocks["reporter"],
                    task_logger=common_mocks["task_logger"],
                    project_id="proj-bad",
                ))


# ════════════════════════════════════════════════════════════════
# Test 7: lifecycle 上报 — _run_graph_agent 包装层验证
# ════════════════════════════════════════════════════════════════


class TestLifecycleReporting:
    """_run_graph_agent (同步 sync 包装) → publish_lifecycle agent_started/done/failed
    用真实 agent_type，不再写死 'ingest'。"""

    def test_lifecycle_reports_correct_agent_type_on_success(self):
        """成功路径: agent_started + agent_done 都带真实 agent_type。"""
        from paper_search.agent.celery_tasks import _run_graph_agent

        mock_reporter = _make_reporter()
        mock_task_logger = _make_task_logger()

        async def fake_async(*args, **kwargs):
            return {"clusters": 5}

        with patch("paper_search.agent.celery_tasks._get_reporter",
                   return_value=mock_reporter), \
             patch("paper_search.agent.celery_tasks._get_logger",
                   return_value=mock_task_logger), \
             patch("paper_search.agent.celery_tasks._run_graph_agent_async",
                   side_effect=fake_async):

            result = _run_graph_agent(
                agent_type="citation_chase",
                arguments={"seed_title": "x"},
                log_id="log-life-1",
                user_query="trace citations of X",
                project_id="proj-life",
            )

        # 应该有两次 publish_lifecycle: agent_started + agent_done
        lc_calls = mock_reporter.publish_lifecycle.call_args_list
        assert len(lc_calls) == 2

        started = lc_calls[0].kwargs
        assert started["agent_type"] == "citation_chase"
        assert started["lifecycle"] == "agent_started"

        done = lc_calls[1].kwargs
        assert done["agent_type"] == "citation_chase"
        assert done["lifecycle"] == "agent_done"
        assert done["result"] == {"clusters": 5}

        assert result == {"result": {"clusters": 5}, "error": ""}

    def test_lifecycle_reports_failure_with_agent_type(self):
        """失败路径: agent_started + agent_failed 都带真实 agent_type。"""
        from paper_search.agent.celery_tasks import _run_graph_agent

        mock_reporter = _make_reporter()
        mock_task_logger = _make_task_logger()

        async def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        with patch("paper_search.agent.celery_tasks._get_reporter",
                   return_value=mock_reporter), \
             patch("paper_search.agent.celery_tasks._get_logger",
                   return_value=mock_task_logger), \
             patch("paper_search.agent.celery_tasks._run_graph_agent_async",
                   side_effect=boom):

            result = _run_graph_agent(
                agent_type="video",
                arguments={"url": "x"},
                log_id="log-life-2",
                user_query="video",
                project_id="proj-life",
            )

        lc_calls = mock_reporter.publish_lifecycle.call_args_list
        assert len(lc_calls) == 2
        assert lc_calls[0].kwargs["lifecycle"] == "agent_started"
        assert lc_calls[0].kwargs["agent_type"] == "video"
        assert lc_calls[1].kwargs["lifecycle"] == "agent_failed"
        assert lc_calls[1].kwargs["agent_type"] == "video"
        assert "kaboom" in lc_calls[1].kwargs["error"]

        assert result == {"error": "kaboom"}


# ════════════════════════════════════════════════════════════════
# Test 8: ingest fallback — sub_agent_task 不路由到 _run_graph_agent
# ════════════════════════════════════════════════════════════════


class TestIngestFallback:
    """agent_type='ingest' 应保留原 7 阶段流水线，不进 _run_graph_agent。"""

    def test_ingest_does_not_route_to_graph_runner(self):
        """sub_agent_task(agent_type='ingest') 不调用 _run_graph_agent。"""
        from paper_search.agent import celery_tasks

        mock_reporter = _make_reporter()
        mock_task_logger = _make_task_logger()

        with patch.object(celery_tasks, "_run_graph_agent") as mock_graph_runner, \
             patch.object(celery_tasks, "_get_reporter",
                          return_value=mock_reporter), \
             patch.object(celery_tasks, "_get_logger",
                          return_value=mock_task_logger), \
             patch.object(celery_tasks, "search_task",
                          return_value={"papers": [], "total": 0}):

            # 用 Celery push_request 注入 request.id；.run() 自动绑定 self=task instance
            celery_tasks.sub_agent_task.push_request(id="celery-task-123")
            try:
                celery_tasks.sub_agent_task.run(
                    user_query="搜 transformer",
                    agent_type="ingest",
                    arguments={},
                    project_id="proj-ingest",
                    agent_task_id="log-ing-1",
                )
            finally:
                celery_tasks.sub_agent_task.pop_request()

        # _run_graph_agent 绝对不应被调用
        mock_graph_runner.assert_not_called()

        # 应有 agent_started lifecycle，agent_type='ingest'
        lc_calls = mock_reporter.publish_lifecycle.call_args_list
        assert any(
            c.kwargs.get("agent_type") == "ingest"
            and c.kwargs.get("lifecycle") == "agent_started"
            for c in lc_calls
        )

    def test_non_ingest_routes_to_graph_runner(self):
        """sub_agent_task(agent_type='clustering') 调用 _run_graph_agent。"""
        from paper_search.agent import celery_tasks

        with patch.object(celery_tasks, "_run_graph_agent",
                          return_value={"result": {"ok": True}, "error": ""}) \
                          as mock_graph_runner:
            celery_tasks.sub_agent_task.push_request(id="celery-task-clu")
            try:
                result = celery_tasks.sub_agent_task.run(
                    user_query="cluster",
                    agent_type="clustering",
                    arguments={"project_id": "proj-clu", "n_clusters": 3},
                    project_id="proj-clu",
                    agent_task_id="log-clu-1",
                )
            finally:
                celery_tasks.sub_agent_task.pop_request()

        mock_graph_runner.assert_called_once()
        kwargs = mock_graph_runner.call_args.kwargs
        assert kwargs["agent_type"] == "clustering"
        assert kwargs["arguments"] == {"project_id": "proj-clu", "n_clusters": 3}
        assert kwargs["log_id"] == "log-clu-1"
        assert kwargs["user_query"] == "cluster"
        assert kwargs["project_id"] == "proj-clu"
        assert result == {"result": {"ok": True}, "error": ""}
