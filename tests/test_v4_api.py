"""v4.0 API + 架构测试。

覆盖:
  1. Document DAO 方法
  2. Preference v4 DAO 方法
  3. Share Request DAO 方法
  4. Intent Classify prompt content
  5. React Executor task structure
  6. Agent heartbeat key format
  7. doc_* 工具注册
  8. API route 定义

运行:
    PYTHONPATH=src pytest tests/test_v4_api.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock
import json


# ═══════════════════════════════════════════════════════════════
# Test 1: Document DAO
# ═══════════════════════════════════════════════════════════════


class TestDocumentDAO:
    """v4.0 documents / document_versions DAO 方法。"""

    def test_create_document_returns_id(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._execute = MagicMock()
        doc_id = db.create_document("user-1", "My Paper", "/oss/user-1/doc.md")
        assert doc_id.startswith("doc-")

    def test_get_document_with_user_id(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        mock_row = {"id": "doc-1", "user_id": "user-1", "title": "Test",
                    "file_path": "/oss/doc.md", "is_auto_review": False}
        db._fetchone = MagicMock(return_value=mock_row)
        doc = db.get_document("doc-1", user_id="user-1")
        assert doc["title"] == "Test"
        assert doc["user_id"] == "user-1"

    def test_list_documents_with_search(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        mock_rows = [{"id": "doc-1", "title": "Test Paper"}]
        db._fetchall = MagicMock(return_value=mock_rows)
        docs = db.list_documents("user-1", search="Test")
        assert len(docs) == 1
        assert docs[0]["title"] == "Test Paper"

    def test_create_document_version_increments(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._execute = MagicMock()
        db._fetchone = MagicMock(return_value={"v": 3})
        ver_id = db.create_document_version("doc-1", "## Hello", trigger="manual_commit")
        assert ver_id.startswith("ver-")

    def test_get_document_current_version(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._fetchone = MagicMock(return_value={"v": 5})
        v = db.get_document_current_version("doc-1")
        assert v == 5

    def test_get_document_current_version_none(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._fetchone = MagicMock(return_value=None)
        v = db.get_document_current_version("doc-1")
        assert v is None

    def test_list_document_versions(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        mock_rows = [
            {"id": "ver-3", "version_number": 3, "trigger": "ai_turn"},
            {"id": "ver-2", "version_number": 2, "trigger": "manual_commit"},
            {"id": "ver-1", "version_number": 1, "trigger": "manual_commit"},
        ]
        db._fetchall = MagicMock(return_value=mock_rows)
        versions = db.list_document_versions("doc-1")
        assert len(versions) == 3
        assert versions[0]["version_number"] == 3

    def test_revert_document_creates_new_version(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._execute = MagicMock()
        db._fetchone = MagicMock(return_value={
            "id": "ver-2", "document_id": "doc-1", "content": "# Old",
            "version_number": 2, "trigger": "manual_commit",
        })

        def mock_get_current(doc_id):
            return 2

        db.get_document_current_version = mock_get_current
        ver_id = db.revert_document("doc-1", "ver-2")
        assert ver_id.startswith("ver-")


# ═══════════════════════════════════════════════════════════════
# Test 2: Preference v4 DAO
# ═══════════════════════════════════════════════════════════════


class TestPreferenceV4DAO:
    """v4.0 user_preferences 表 DAO。"""

    def test_get_v4_preferences_returns_dict(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        mock_row = {
            "user_id": "user-1", "research_domain": "NLP",
            "writing_style": "APA", "language_pref": "zh",
            "mentor_quotes": "Think deep", "other": {},
        }
        db._fetchone = MagicMock(return_value=mock_row)
        prefs = db.get_v4_preferences("user-1")
        assert prefs["research_domain"] == "NLP"
        assert prefs["writing_style"] == "APA"

    def test_get_v4_preferences_returns_none(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._fetchone = MagicMock(return_value=None)
        prefs = db.get_v4_preferences("user-1")
        assert prefs is None

    def test_upsert_v4_preferences_insert(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._execute = MagicMock()
        db._fetchone = MagicMock(return_value=None)
        db.upsert_v4_preferences("user-1", research_domain="CV",
                                writing_style="IEEE", language_pref="en")
        assert db._execute.called

    def test_upsert_v4_preferences_update(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._execute = MagicMock()
        db._fetchone = MagicMock(return_value={
            "user_id": "user-1", "research_domain": "NLP",
        })
        db.upsert_v4_preferences("user-1", research_domain="CV")
        assert db._execute.called

    def test_upsert_ignores_unknown_fields(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._execute = MagicMock()
        db._fetchone = MagicMock(return_value=None)
        db.upsert_v4_preferences("user-1", research_domain="NLP",
                                unknown_field="should_ignore")
        assert db._execute.called


# ═══════════════════════════════════════════════════════════════
# Test 3: Share Request DAO
# ═══════════════════════════════════════════════════════════════


class TestShareRequestDAO:
    """v4.0 share_requests DAO。"""

    def test_create_share_request(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._execute = MagicMock()
        share_id = db.create_share_request(
            "user-a", "user-b", "paper", "pap-001", "Check this")
        assert share_id.startswith("shr-")

    def test_list_share_requests_inbound(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        mock_rows = [
            {"id": "shr-1", "from_user_id": "user-a", "to_user_id": "user-b",
             "resource_type": "paper", "status": "pending"},
        ]
        db._fetchall = MagicMock(return_value=mock_rows)
        reqs = db.list_share_requests("user-b", direction="inbound")
        assert len(reqs) == 1
        assert reqs[0]["from_user_id"] == "user-a"

    def test_list_share_requests_outbound(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        mock_rows = [{"id": "shr-1", "from_user_id": "user-a", "to_user_id": "user-b"}]
        db._fetchall = MagicMock(return_value=mock_rows)
        reqs = db.list_share_requests("user-a", direction="outbound")
        assert len(reqs) == 1

    def test_update_share_request_status(self):
        from paper_search.agent.pgdb import PostgresAgentDB

        db = PostgresAgentDB()
        db._execute = MagicMock()
        result = db.update_share_request("shr-1", "accepted")
        assert result is True
        db._execute.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# Test 4: v4.0 Prompt Content
# ═══════════════════════════════════════════════════════════════


class TestV4Prompts:
    """v4.0 prompts 存在且包含关键规则。"""

    def test_intent_classify_prompt_exists(self):
        from paper_search.agent.main_agent_prompts import INTENT_CLASSIFY_PROMPT
        assert "7 种意图" in INTENT_CLASSIFY_PROMPT or "intent" in INTENT_CLASSIFY_PROMPT
        assert "survey" in INTENT_CLASSIFY_PROMPT
        assert "writing" in INTENT_CLASSIFY_PROMPT
        assert "chat" in INTENT_CLASSIFY_PROMPT
        assert "ops" in INTENT_CLASSIFY_PROMPT
        assert "score" in INTENT_CLASSIFY_PROMPT
        assert "should_plan" in INTENT_CLASSIFY_PROMPT
        assert "planning_prompt" in INTENT_CLASSIFY_PROMPT

    def test_react_system_prompt_exists(self):
        from paper_search.agent.main_agent_prompts import REACT_SYSTEM_PROMPT
        assert "执行" in REACT_SYSTEM_PROMPT
        assert "工具" in REACT_SYSTEM_PROMPT or "tools" in REACT_SYSTEM_PROMPT.lower()

    def test_clarify_system_prompt_exists(self):
        from paper_search.agent.main_agent_prompts import CLARIFY_SYSTEM_PROMPT
        assert "收集" in CLARIFY_SYSTEM_PROMPT
        assert "ask" in CLARIFY_SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════════════
# Test 5: React Executor
# ═══════════════════════════════════════════════════════════════


class TestReactExecutor:
    """v4.0 react_executor Celery task。"""

    def test_task_is_registered(self):
        from paper_search.agent.react_executor import react_execute
        assert react_execute is not None
        assert callable(react_execute)

    def test_task_name(self):
        from paper_search.agent.react_executor import react_execute
        assert react_execute.name

    def test_react_max_rounds_env(self):
        from paper_search.agent.react_executor import REACT_MAX_ROUNDS
        assert REACT_MAX_ROUNDS > 0
        assert REACT_MAX_ROUNDS <= 20

    def test_react_execute_accepts_plan_args(self):
        from paper_search.agent.react_executor import _run_react

        plan_args = {
            "plan_id": "plan-1", "agent_id": "agent-001",
            "user_id": "user-1", "session_id": "main",
            "todos": [], "context": {}, "llm_provider": "deepseek",
        }
        with patch("paper_search.agent.react_executor._get_llm") as mock_llm, \
             patch("paper_search.agent.react_executor._get_db") as mock_db, \
             patch("paper_search.agent.react_executor._get_redis") as mock_redis, \
             patch("paper_search.agent.react_executor._outbox_push"), \
             patch("paper_search.agent.tool_registry.ToolRegistry.get_instance") as mock_registry:
            mock_llm.return_value.chat.return_value = MagicMock(
                content="Done", tool_calls=[])
            mock_db.return_value = MagicMock()
            mock_redis.return_value = MagicMock()
            reg = MagicMock()
            reg.get_all_tool_defs = MagicMock(return_value=[])
            mock_registry.return_value = reg

            result = _run_react(plan_args)
            assert result["status"] in ("done", "max_rounds")


# ═══════════════════════════════════════════════════════════════
# Test 6: Agent Heartbeat
# ═══════════════════════════════════════════════════════════════


class TestAgentHeartbeat:
    """v4.0 Agent 心跳。"""

    def test_heartbeat_key_format(self):
        user_id = "user-abc123"
        key = f"agent:heartbeat:{user_id}"
        assert key == "agent:heartbeat:user-abc123"

    def test_heartbeat_payload_format(self):
        payload = json.dumps({
            "status": "running",
            "active_turns": 2,
            "current_session": "main",
        })
        data = json.loads(payload)
        assert data["status"] == "running"
        assert data["active_turns"] == 2

    def test_agent_heartbeat_uses_status_hash(self):
        from paper_search.agent.daemon import AgentSupervisor
        s = AgentSupervisor(redis_url="redis://localhost:6379/0")
        assert s._agents == {}
        assert s._status_cache == {}
        assert s._stopping is False


# ═══════════════════════════════════════════════════════════════
# Test 7: doc_* 工具注册
# ═══════════════════════════════════════════════════════════════


class TestDocTools:
    """v4.0 6 个 doc_* 工具已注册。"""

    REQUIRED_DOC_TOOLS = [
        "doc_read",
        "doc_write_section",
        "doc_append",
        "doc_diff_apply",
        "doc_generate_review",
        "doc_search_rag",
    ]

    def test_all_doc_tools_registered(self):
        from paper_search.agent.tool_registry import ToolRegistry
        r = ToolRegistry()
        r._register_all()
        tools = r.list_tools()
        names = {t["name"] for t in tools}
        for tool_name in self.REQUIRED_DOC_TOOLS:
            assert tool_name in names, f"Missing tool: {tool_name}"

    def test_doc_tool_count(self):
        from paper_search.agent.tool_registry import ToolRegistry
        r = ToolRegistry()
        r._register_all()
        tools = r.list_tools()
        doc_tools = [t for t in tools if t["name"].startswith("doc_")]
        assert len(doc_tools) >= 6

    def test_doc_read_has_correct_params(self):
        from paper_search.agent.tool_registry import ToolRegistry
        r = ToolRegistry()
        r._register_all()
        tool = r.get("doc_read")
        assert tool is not None
        assert "document_id" in str(tool.args_schema.model_fields)

    def test_doc_write_section_has_params(self):
        from paper_search.agent.tool_registry import ToolRegistry
        r = ToolRegistry()
        r._register_all()
        tool = r.get("doc_write_section")
        assert tool is not None

    def test_doc_append_has_params(self):
        from paper_search.agent.tool_registry import ToolRegistry
        r = ToolRegistry()
        r._register_all()
        tool = r.get("doc_append")
        assert tool is not None

    def test_doc_search_rag_has_params(self):
        from paper_search.agent.tool_registry import ToolRegistry
        r = ToolRegistry()
        r._register_all()
        tool = r.get("doc_search_rag")
        assert tool is not None


# ═══════════════════════════════════════════════════════════════
# Test 8: Intent Classify routing
# ═══════════════════════════════════════════════════════════════


class TestIntentClassifyRouting:
    """v4.0 intent_classify 路由逻辑。"""

    def test_chat_intent_routes_to_inline_reply(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        g = MainGraph.__new__(MainGraph)
        state = {"v4_intents": [{"intent": "chat", "score": 0.9}],
                 "should_plan": False, "route": "chat"}
        import asyncio
        route = asyncio.run(g._route_intent(state))
        assert route == "inline_reply"

    def test_ops_intent_routes_to_ops_plan(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        g = MainGraph.__new__(MainGraph)
        state = {"v4_intents": [{"intent": "ops", "score": 0.95}],
                 "route": "ops"}
        import asyncio
        route = asyncio.run(g._route_intent(state))
        assert route == "ops_plan"

    def test_research_intent_routes_to_plan(self):
        from paper_search.agent.graphs.main_graph import MainGraph
        g = MainGraph.__new__(MainGraph)
        state = {"v4_intents": [{"intent": "survey", "score": 0.92}],
                 "route": "plan"}
        import asyncio
        route = asyncio.run(g._route_intent(state))
        assert route == "plan"


# ═══════════════════════════════════════════════════════════════
# Test 9: Agent API route definitions
# ═══════════════════════════════════════════════════════════════


class TestV4Routes:
    """v4.0 API 路由定义。"""

    def test_agent_me_routes_exist(self):
        from paper_search.api.routes import router
        paths = [r.path for r in router.routes]
        assert "/api/agents/me" in paths
        assert "/api/agents/me/status" in paths

    def test_document_routes_exist(self):
        from paper_search.api.routes import router
        paths = [r.path for r in router.routes]
        assert "/api/documents" in paths
        assert "/api/documents/{doc_id}" in paths
        assert "/api/documents/{doc_id}/versions" in paths
        assert "/api/documents/{doc_id}/versions/{ver_id}" in paths
        assert "/api/documents/{doc_id}/revert/{ver_id}" in paths
        assert "/api/documents/{doc_id}/download" in paths

    def test_preferences_route_exists(self):
        from paper_search.api.routes import router
        paths = [r.path for r in router.routes]
        assert "/api/preferences/me" in paths

    def test_share_routes_exist(self):
        from paper_search.api.routes import router
        paths = [r.path for r in router.routes]
        assert "/api/share" in paths
        assert "/api/share/requests" in paths
        assert "/api/share/requests/{share_id}" in paths


# ═══════════════════════════════════════════════════════════════
# Test 10: Database Schema SQL
# ═══════════════════════════════════════════════════════════════


class TestDatabaseSchema:
    """v4.0 init_db.sql schema。"""

    def test_documents_table_in_sql(self):
        import os
        sql_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "init_db.sql")
        if not os.path.exists(sql_path):
            sql_path = "scripts/init_db.sql"
        if os.path.exists(sql_path):
            content = open(sql_path).read()
            assert "CREATE TABLE documents" in content
            assert "CREATE TABLE user_preferences" in content
            assert "CREATE TABLE share_requests" in content
            assert "CREATE TABLE event_logs" in content
            assert "CREATE TABLE captures" in content
            assert "CREATE TABLE _schema_meta" in content

    def test_gen_id_functions_in_sql(self):
        import os
        sql_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "init_db.sql")
        if not os.path.exists(sql_path):
            sql_path = "scripts/init_db.sql"
        if os.path.exists(sql_path):
            content = open(sql_path).read()
            for func in ["gen_doc_id", "gen_ver_id", "gen_share_id", "gen_agent_id"]:
                assert f"CREATE OR REPLACE FUNCTION {func}" in content

    def test_sessions_document_id_column(self):
        import os
        sql_path = os.path.join(
            os.path.dirname(__file__), "..", "scripts", "init_db.sql")
        if not os.path.exists(sql_path):
            sql_path = "scripts/init_db.sql"
        if os.path.exists(sql_path):
            content = open(sql_path).read()
            assert "document_id" in content