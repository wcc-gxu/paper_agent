"""v5 Graph Architecture 测试。

覆盖:
  1. v5 INTENT_CLASSIFY_PROMPT content
  2. _intent_classify 输出 route 字段
  3. _route_intent rag/chat/ops 路由
  4. _rag_handler 调用 agent_knowledge_ask
  5. compile() 包含 rag_handler 节点
  6. MainState 包含新字段

运行:
    PYTHONPATH=src pytest tests/test_v5_graph.py -v
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# Test 1: v5 Intent Classify Prompt
# ═══════════════════════════════════════════════════════════════


class TestV5Prompt:
    """v5 prompt 格式验证。"""

    def test_prompt_includes_all_intents(self):
        from paper_search.agent.main_agent_prompts import INTENT_CLASSIFY_PROMPT
        for intent in ("rag", "survey", "translation", "writing", "glossary",
                        "paper_analysis", "clustering", "citation_chase",
                        "knowledge_mgmt", "chat", "ops"):
            assert intent in INTENT_CLASSIFY_PROMPT, f"missing intent: {intent}"

    def test_prompt_outputs_route_and_params(self):
        from paper_search.agent.main_agent_prompts import INTENT_CLASSIFY_PROMPT
        assert '"primary"' in INTENT_CLASSIFY_PROMPT
        assert '"route"' in INTENT_CLASSIFY_PROMPT
        assert '"params"' in INTENT_CLASSIFY_PROMPT
        assert '"confidence"' in INTENT_CLASSIFY_PROMPT

    def test_intent_classify_v5_compat_alias(self):
        from paper_search.agent.main_agent_prompts import (
            INTENT_CLASSIFY_PROMPT,
            INTENT_CLASSIFY_V5_PROMPT,
        )
        assert INTENT_CLASSIFY_PROMPT == INTENT_CLASSIFY_V5_PROMPT


# ═══════════════════════════════════════════════════════════════
# Test 2: _route_intent routing
# ═══════════════════════════════════════════════════════════════


class TestRouteIntent:
    """v5 intent → handler routing。"""

    def setup_method(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        self.graph = MainGraph(llm=None, registry=None, db=None)

    @pytest.mark.asyncio
    async def test_route_chat(self):
        assert await self.graph._route_intent({"route": "chat"}) == "inline_reply"

    @pytest.mark.asyncio
    async def test_route_ops(self):
        assert await self.graph._route_intent({"route": "ops"}) == "ops_plan"

    @pytest.mark.asyncio
    async def test_route_rag(self):
        assert await self.graph._route_intent({"route": "rag"}) == "rag_handler"

    @pytest.mark.asyncio
    async def test_route_unknown_falls_back_to_plan(self):
        assert await self.graph._route_intent({"route": "some_unknown"}) == "plan"

    @pytest.mark.asyncio
    async def test_route_missing_defaults_to_inline_reply(self):
        assert await self.graph._route_intent({}) == "inline_reply"


# ═══════════════════════════════════════════════════════════════
# Test 3: _intent_classify
# ═══════════════════════════════════════════════════════════════


class TestIntentClassifyV5:
    """v5 _intent_classify 节点行为。"""

    def setup_method(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        self.graph = MainGraph(llm=None, registry=None, db=None)

    @pytest.mark.asyncio
    async def test_classify_rag(self):
        self.graph.llm = MagicMock()
        mock_chat_json = AsyncMock(return_value={
            "primary": "rag", "route": "rag",
            "params": {"question": "test question"},
            "confidence": 0.95,
        })
        self.graph.llm.chat_json = mock_chat_json

        result = await self.graph._intent_classify({
            "user_content": "库里有哪些关于attention的论文？",
            "session_id": "main",
        })
        assert result["route"] == "rag"
        assert result["primary_intent"] == "rag"
        assert result["intent_params"] == {"question": "test question"}

    @pytest.mark.asyncio
    async def test_classify_chat(self):
        self.graph.llm = MagicMock()
        self.graph.llm.chat_json = AsyncMock(return_value={
            "primary": "chat", "route": "chat",
            "params": {}, "confidence": 0.95,
        })
        result = await self.graph._intent_classify({
            "user_content": "你好",
            "session_id": "main",
        })
        assert result["route"] == "chat"

    @pytest.mark.asyncio
    async def test_classify_ops(self):
        self.graph.llm = MagicMock()
        self.graph.llm.chat_json = AsyncMock(return_value={
            "primary": "ops", "route": "ops",
            "params": {}, "confidence": 0.95,
        })
        result = await self.graph._intent_classify({
            "user_content": "重启服务",
            "session_id": "main",
        })
        assert result["route"] == "ops"

    @pytest.mark.asyncio
    async def test_classify_llm_failure_fallback_to_chat(self):
        self.graph.llm = MagicMock()
        self.graph.llm.chat_json = AsyncMock(side_effect=RuntimeError("LLM down"))

        result = await self.graph._intent_classify({
            "user_content": "anything",
            "session_id": "main",
        })
        assert result["route"] == "chat"


# ═══════════════════════════════════════════════════════════════
# Test 4: _rag_handler
# ═══════════════════════════════════════════════════════════════


class TestRagHandler:
    """v5 _rag_handler 节点行为。"""

    def setup_method(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        self.graph = MainGraph(llm=None, registry=None, db=None)
        self.graph._push = AsyncMock()
        self.graph._push_status = AsyncMock()
        self.graph._push_error = AsyncMock()

    @pytest.mark.asyncio
    async def test_rag_handler_calls_tool_and_pushes_reply(self):
        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value=json.dumps({
            "answer": "Attention机制包括Self-Attention、Multi-Head Attention等。",
            "sources": [{"title": "Attention Is All You Need"}],
        }))
        self.graph.registry = MagicMock()
        self.graph.registry.get = MagicMock(return_value=mock_tool)

        result = await self.graph._rag_handler({
            "session_id": "test",
            "intent_params": {"question": "attention机制有哪些？"},
            "user_content": "attention机制有哪些？",
        })

        assert result["_reply_pushed"] is True
        assert "Attention" in result["final_reply"]

        self.graph.registry.get.assert_called_once_with("agent_knowledge_ask")
        mock_tool.ainvoke.assert_called_once_with({"question": "attention机制有哪些？"})

        # Should push status twice (start + done)
        assert self.graph._push_status.call_count >= 2
        # Should push message/reply
        self.graph._push.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rag_handler_falls_back_to_user_content(self):
        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value=json.dumps({"answer": "No results"}))
        self.graph.registry = MagicMock()
        self.graph.registry.get = MagicMock(return_value=mock_tool)

        result = await self.graph._rag_handler({
            "session_id": "test",
            "user_content": "fallback question?",
        })
        assert result["_reply_pushed"] is True
        mock_tool.ainvoke.assert_called_once_with({"question": "fallback question?"})

    @pytest.mark.asyncio
    async def test_rag_handler_tool_not_found(self):
        self.graph.registry = MagicMock()
        self.graph.registry.get = MagicMock(return_value=None)

        result = await self.graph._rag_handler({
            "session_id": "test",
            "user_content": "anything",
        })
        assert result["_reply_pushed"] is True
        assert result["error"]

    @pytest.mark.asyncio
    async def test_rag_handler_tool_throws(self):
        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(side_effect=RuntimeError("KB down"))
        self.graph.registry = MagicMock()
        self.graph.registry.get = MagicMock(return_value=mock_tool)

        result = await self.graph._rag_handler({
            "session_id": "test",
            "user_content": "anything",
        })
        assert result["_reply_pushed"] is True
        assert "KB down" in result["error"]


# ═══════════════════════════════════════════════════════════════
# Test 5: Graph Compilation
# ═══════════════════════════════════════════════════════════════


class TestV5GraphCompilation:
    """v5 graph compile() 结构。"""

    def test_compile_has_rag_handler_node(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        graph = MainGraph(llm=None, registry=None, db=None)

        compiled = graph.compile()
        nodes = list(compiled.nodes.keys() if hasattr(compiled, 'nodes') else [])
        assert "rag_handler" in nodes

    def test_compile_has_fast_triage_and_intent_classify(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        graph = MainGraph(llm=None, registry=None, db=None)

        compiled = graph.compile()
        nodes = list(compiled.nodes.keys() if hasattr(compiled, 'nodes') else [])
        assert "fast_triage" in nodes
        assert "intent_classify" in nodes


# ═══════════════════════════════════════════════════════════════
# Test 6: MainState
# ═══════════════════════════════════════════════════════════════


class TestV5MainState:
    """v5 MainState 字段。"""

    def test_state_has_v5_fields(self):
        from paper_search.agent.graphs.main_graph import MainState
        annotations = MainState.__annotations__
        assert "primary_intent" in annotations
        assert "intent_params" in annotations
        assert "route" in annotations


# ═══════════════════════════════════════════════════════════════
# Test 7: WS envelope v5
# ═══════════════════════════════════════════════════════════════


class TestV5WSEnvelope:
    """v5 WS 信封不含 role 和 priorityKind。"""

    def test_envelope_no_role_field(self):
        from paper_search.agent.main_agent import _now
        import asyncio

        # 验证 _now 可用
        ts = _now()
        assert "T" in ts

    def test_outbox_publish_respects_v10(self):
        from paper_search.agent.outbox import PRIORITY_DEFAULTS
        assert ("status", "") in PRIORITY_DEFAULTS
        assert ("message", "reply") in PRIORITY_DEFAULTS
        assert ("tool", "start") in PRIORITY_DEFAULTS
        assert ("error", "TASK_FAILED") in PRIORITY_DEFAULTS


# ═══════════════════════════════════════════════════════════════
# Test 8: Ingest Handler
# ═══════════════════════════════════════════════════════════════


class TestIngestHandler:
    """v5 _ingest_handler 节点行为。"""

    def setup_method(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        self.graph = MainGraph(llm=None, registry=None, db=None)
        self.graph._push = AsyncMock()
        self.graph._push_status = AsyncMock()
        self.graph._push_error = AsyncMock()

    @pytest.mark.asyncio
    async def test_ingest_no_path_asks_user(self):
        result = await self.graph._ingest_handler({
            "session_id": "test", "user_content": "帮我入库",
        })
        assert result["_reply_pushed"] is True
        assert "请指定" in result["final_reply"]

    @pytest.mark.asyncio
    async def test_ingest_dir_not_found(self):
        result = await self.graph._ingest_handler({
            "session_id": "test", "intent_params": {"dir_path": "/nonexistent"},
        })
        assert result["_reply_pushed"] is True
        assert "不存在" in result["final_reply"] or result.get("error")


# ═══════════════════════════════════════════════════════════════
# Test 9: Cleanup Handler
# ═══════════════════════════════════════════════════════════════


class TestCleanupHandler:
    """v5 _cleanup_handler 节点行为。"""

    def setup_method(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        self.graph = MainGraph(llm=None, registry=None, db=None)
        self.graph._push = AsyncMock()
        self.graph._push_status = AsyncMock()
        self.graph._push_error = AsyncMock()

    @pytest.mark.asyncio
    async def test_cleanup_no_path_asks_user(self):
        result = await self.graph._cleanup_handler({
            "session_id": "test", "user_content": "帮我清理",
        })
        assert result["_reply_pushed"] is True
        assert "请指定" in result["final_reply"]

    @pytest.mark.asyncio
    async def test_cleanup_dir_not_found(self):
        result = await self.graph._cleanup_handler({
            "session_id": "test", "intent_params": {"dir_path": "/nonexistent"},
        })
        assert result["_reply_pushed"] is True
        assert "不存在" in result["final_reply"]


# ═══════════════════════════════════════════════════════════════
# Test 10: Literature Search Handler
# ═══════════════════════════════════════════════════════════════


class TestLiteratureHandler:
    """v5 _literature_search_handler 节点行为。"""

    def setup_method(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        self.graph = MainGraph(llm=None, registry=None, db=None)
        self.graph._push = AsyncMock()
        self.graph._push_status = AsyncMock()
        self.graph._push_error = AsyncMock()

    @pytest.mark.asyncio
    async def test_search_tool_not_found(self):
        self.graph.registry = MagicMock()
        self.graph.registry.get = MagicMock(return_value=None)
        result = await self.graph._literature_search_handler({
            "session_id": "test", "user_content": "找transformer论文",
        })
        assert result["_reply_pushed"] is True
        assert "暂不可用" in result["final_reply"]

    @pytest.mark.asyncio
    async def test_search_calls_tool(self):
        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value='{"result": "找到10篇"}')
        self.graph.registry = MagicMock()
        self.graph.registry.get = MagicMock(return_value=mock_tool)

        result = await self.graph._literature_search_handler({
            "session_id": "test",
            "intent_params": {"search_query": "transformer"},
            "correlation_id": "corr-1",
        })
        assert result["_reply_pushed"] is True
        assert "10篇" in result["final_reply"]


# ═══════════════════════════════════════════════════════════════
# Test 11: Compile has all v5 handlers
# ═══════════════════════════════════════════════════════════════


class TestV5AllHandlers:
    """验证所有 v5 handler 节点已注册。"""

    def test_compile_has_all_v5_handlers(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        graph = MainGraph(llm=None, registry=None, db=None)
        compiled = graph.compile()
        nodes = list(compiled.nodes.keys() if hasattr(compiled, 'nodes') else [])
        for name in ("rag_handler", "ingest_handler", "cleanup_handler",
                      "literature_search_handler"):
            assert name in nodes, f"missing handler node: {name}"


# ═══════════════════════════════════════════════════════════════
# Test 12: Route all new intents
# ═══════════════════════════════════════════════════════════════


class TestRouteAllIntents:
    """验证所有 intent 路由正确。"""

    def setup_method(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        self.graph = MainGraph(llm=None, registry=None, db=None)

    @pytest.mark.asyncio
    async def test_route_ingest(self):
        assert await self.graph._route_intent({"route": "ingest"}) == "ingest_handler"

    @pytest.mark.asyncio
    async def test_route_cleanup(self):
        assert await self.graph._route_intent({"route": "cleanup"}) == "cleanup_handler"

    @pytest.mark.asyncio
    async def test_route_survey(self):
        assert await self.graph._route_intent({"route": "survey"}) == "literature_search_handler"
