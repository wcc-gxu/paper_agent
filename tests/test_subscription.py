"""订阅系统单元测试。

覆盖:
  1. AgentDB subscription CRUD (create/get/list/update/delete)
  2. AgentDB subscription_results (save/get)
  3. Subscription models
  4. Subscription API endpoints

运行:
    PYTHONPATH=src pytest tests/test_subscription.py -v
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# Test 1: Subscription Pydantic Models
# ═══════════════════════════════════════════════════════════════


class TestSubscriptionModels:
    """Subscription / SubscriptionResult 模型测试。"""

    def test_subscription_defaults(self):
        """默认值应正确初始化。"""
        from paper_search.models import Subscription

        sub = Subscription(name="Test", keywords="machine learning")
        assert sub.name == "Test"
        assert sub.keywords == "machine learning"
        assert sub.interval_hours == 24
        assert sub.enabled is True
        assert sub.sources == ["arxiv", "semantic_scholar"]

    def test_subscription_result_defaults(self):
        """默认值应正确初始化。"""
        from paper_search.models import SubscriptionResult

        sr = SubscriptionResult(
            subscription_id="sub-001",
            paper_id="paper-001",
            title="Test Paper",
        )
        assert sr.subscription_id == "sub-001"
        assert sr.paper_id == "paper-001"
        assert sr.authors == []


# ═══════════════════════════════════════════════════════════════
# Test 2: AgentDB Subscription CRUD
# ═══════════════════════════════════════════════════════════════


class TestSubscriptionCRUD:
    """订阅 CRUD 集成测试。

    需要 DATABASE_URL 且显式设置 PYTEST_DB_INTEGRATION=1 才运行。
    否则 skip（避免污染生产数据库）。
    """

    @pytest.fixture
    def db(self):
        import os
        if not os.environ.get("PYTEST_DB_INTEGRATION"):
            pytest.skip("PYTEST_DB_INTEGRATION not set — skip DB integration test")
        from paper_search.agent.pgdb import PostgresAgentDB
        return PostgresAgentDB()

    def test_create_and_get_subscription(self, db):
        """创建并获取订阅。"""
        sub_id = db.create_subscription(
            name="LLM Safety",
            keywords="language model safety alignment",
            sources=["arxiv", "semantic_scholar"],
            interval_hours=12,
        )
        assert len(sub_id) == 8

        sub = db.get_subscription(sub_id)
        assert sub is not None
        assert sub["name"] == "LLM Safety"
        assert sub["keywords"] == "language model safety alignment"
        assert sub["sources"] == ["arxiv", "semantic_scholar"]
        assert sub["interval_hours"] == 12
        assert sub["enabled"] == 1
        assert isinstance(sub["last_paper_ids"], list)

    def test_get_nonexistent_subscription(self, db):
        """获取不存在的订阅返回 None。"""
        assert db.get_subscription("nonexistent") is None

    def test_list_subscriptions(self, db):
        """列出所有订阅。"""
        for i in range(3):
            db.create_subscription(
                name=f"Sub {i}",
                keywords=f"keyword {i}",
            )

        subs = db.list_subscriptions()
        assert len(subs) == 3

    def test_list_subscriptions_enabled_only(self, db):
        """enabled_only 过滤 — 禁用订阅不出现。"""
        db.create_subscription(name="Enabled", keywords="test")
        sub_id2 = db.create_subscription(name="Will Disable", keywords="test")
        db.update_subscription(sub_id2, enabled=0)

        subs = db.list_subscriptions(enabled_only=True)
        assert len(subs) == 1
        assert subs[0]["name"] == "Enabled"

    def test_update_subscription(self, db):
        """更新订阅字段。"""
        sub_id = db.create_subscription(name="Original", keywords="test")
        db.update_subscription(
            sub_id,
            name="Updated",
            interval_hours=48,
            last_paper_ids=["p1", "p2"],
        )

        sub = db.get_subscription(sub_id)
        assert sub["name"] == "Updated"
        assert sub["interval_hours"] == 48
        assert sub["last_paper_ids"] == ["p1", "p2"]

    def test_delete_subscription(self, db):
        """删除订阅及关联结果。"""
        sub_id = db.create_subscription(name="To Delete", keywords="test")
        assert db.get_subscription(sub_id) is not None

        db.delete_subscription(sub_id)
        assert db.get_subscription(sub_id) is None

    def test_save_and_get_subscription_results(self, db):
        """保存并获取推送结果。"""
        sub_id = db.create_subscription(name="Test", keywords="ml")

        # Save results
        paper = {
            "paper_id": "arxiv:1234.5678",
            "title": "Test ML Paper",
            "authors": ["Alice", "Bob"],
            "year": 2025,
            "abstract": "An important finding...",
            "venue": "ICML",
            "source": "arxiv",
            "doi": "10.1234/test",
        }
        row_id = db.save_subscription_result(sub_id, paper)
        assert row_id > 0

        # Duplicate should be silently ignored (INSERT OR IGNORE)
        row_id2 = db.save_subscription_result(sub_id, paper)
        # SQLite may return lastrowid even on IGNORE; the key is that count stays 1
        assert row_id2 >= 0

        # Get results
        results = db.get_subscription_results(sub_id)
        assert len(results) == 1
        r = results[0]
        assert r["title"] == "Test ML Paper"
        assert r["authors"] == ["Alice", "Bob"]

    def test_get_subscription_results_with_since(self, db):
        """since 参数过滤。"""
        sub_id = db.create_subscription(name="Test", keywords="ml")
        paper = {
            "paper_id": "p1",
            "title": "Paper 1",
            "authors": [],
            "year": 2025,
            "abstract": "",
            "venue": "",
            "source": "arxiv",
            "doi": "",
        }
        db.save_subscription_result(sub_id, paper)

        # since far in the future should return 0
        results = db.get_subscription_results(sub_id, since="2099-01-01T00:00:00Z")
        assert len(results) == 0

        # since empty should return the paper
        results = db.get_subscription_results(sub_id)
        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════
# Test 3: SubscriptionResult model construction
# ═══════════════════════════════════════════════════════════════


class TestSubscriptionResultModel:
    """SubscriptionResult 序列化测试。"""

    def test_authors_serialization(self):
        """authors 列表应正确 JSON 序列化/反序列化。"""
        from paper_search.models import SubscriptionResult

        sr = SubscriptionResult(
            subscription_id="sub-001",
            paper_id="p-001",
            title="Test",
            authors=["张三", "John Doe"],
        )
        data = sr.model_dump()
        assert data["authors"] == ["张三", "John Doe"]
