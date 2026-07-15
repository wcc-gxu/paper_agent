"""Celery Task 单元测试。

覆盖:
  1. search_task — mock PaperSearchEngine
  2. evaluate_task — mock LLMClientV2
  3. rank_task — mock JournalRanker
  4. subscription_check_task — mock engine + DB
  5. PipelineRunner.run_pipeline_via_celery

运行:
    PYTHONPATH=src pytest tests/test_celery_tasks.py -v
"""

from unittest.mock import MagicMock, patch


# ═══════════════════════════════════════════════════════════════
# Test 1: search_task
# ═══════════════════════════════════════════════════════════════


class TestSearchTask:
    """search_task 单元测试。"""

    def test_search_task_returns_papers(self):
        """验证搜索任务返回正确格式。"""
        from paper_search.agent.celery_tasks import search_task

        mock_paper = MagicMock()
        mock_paper.title = "Test Paper"
        mock_paper.year = 2024
        mock_paper.abstract = "Test abstract"
        mock_paper.authors = ["Author A"]
        mock_paper.venue = "Test Venue"
        mock_paper.doi = "10.1234/test"
        mock_paper.source_url = ""
        mock_paper.source = MagicMock()
        mock_paper.source.value = "arxiv"

        mock_result = MagicMock()
        mock_result.papers = [mock_paper]

        # Mock DB
        mock_db = MagicMock()
        mock_db.upsert_paper.return_value = "test-paper-id"
        mock_db._now.return_value = "2025-01-01T00:00:00Z"

        with patch("paper_search.agent.celery_tasks._get_db", return_value=mock_db):
            with patch("paper_search.engine.PaperSearchEngine") as mock_eng_cls:
                mock_eng = MagicMock()
                mock_eng_cls.return_value = mock_eng
                with patch("asyncio.new_event_loop") as mock_new_loop:
                    mock_loop = MagicMock()
                    mock_loop.run_until_complete.return_value = mock_result
                    mock_new_loop.return_value = mock_loop

                    result = search_task(user_query="test query", max_results=5)

        assert result["total"] == 1
        assert result["papers"][0]["title"] == "Test Paper"
        assert result["error"] == ""


# ═══════════════════════════════════════════════════════════════
# Test 2: evaluate_task
# ═══════════════════════════════════════════════════════════════


class TestEvaluateTask:
    """evaluate_task 单元测试。"""

    def test_evaluate_task_returns_scores(self):
        """验证评估任务返回评分。"""
        from paper_search.agent.celery_tasks import evaluate_task

        mock_eval = MagicMock()
        mock_eval.score = 0.85
        mock_eval.reason = "Relevant"

        with patch("paper_search.agent.llm_client_v2.LLMClientV2") as mock_llm_cls:
            mock_llm = MagicMock()
            mock_llm_cls.return_value = mock_llm
            with patch("asyncio.new_event_loop") as mock_new_loop:
                mock_loop = MagicMock()
                mock_loop.run_until_complete.return_value = [mock_eval]
                mock_new_loop.return_value = mock_loop

                result = evaluate_task(
                    user_query="test",
                    papers=[{"paper_id": "p1", "title": "T", "abstract": "A"}],
                )

        assert result["relevant_count"] == 1
        assert len(result["evaluations"]) == 1

    def test_evaluate_task_empty_papers(self):
        """空论文列表返回空。"""
        from paper_search.agent.celery_tasks import evaluate_task
        result = evaluate_task(user_query="test", papers=[])
        assert result["evaluations"] == []
        assert result["relevant_count"] == 0


# ═══════════════════════════════════════════════════════════════
# Test 3: rank_task
# ═══════════════════════════════════════════════════════════════


class TestRankTask:
    """rank_task 单元测试。"""

    def test_rank_task_returns_ranks(self):
        """有 venue 的论文返回等级。"""
        from paper_search.agent.celery_tasks import rank_task

        mock_rank = MagicMock()
        mock_rank.unified_level = "A"
        mock_rank.ccf_level = "A"
        mock_rank.sci_zone = "Q1"

        mock_db = MagicMock()

        with patch("paper_search.agent.journal_ranker.JournalRanker") as mock_r_cls, \
             patch("paper_search.agent.celery_tasks._get_db", return_value=mock_db):
            mock_r = MagicMock()
            mock_r.rank.return_value = mock_rank
            mock_r_cls.return_value = mock_r

            result = rank_task(papers=[{"paper_id": "p1", "venue": "Nature"}])

        assert result["ranks"][0]["level"] == "A"

    def test_rank_task_no_venue(self):
        """无 venue 跳过。"""
        from paper_search.agent.celery_tasks import rank_task
        result = rank_task(papers=[{"paper_id": "p1", "venue": ""}])
        assert result["ranks"] == []


# ═══════════════════════════════════════════════════════════════
# Test 4: subscription_check_task
# ═══════════════════════════════════════════════════════════════


class TestSubscriptionCheckTask:
    """subscription_check_task 测试。"""

    def test_no_subscriptions(self):
        """无订阅时返回 0。"""
        from paper_search.agent.celery_tasks import subscription_check_task

        mock_db = MagicMock()
        mock_db.list_subscriptions.return_value = []
        mock_db._now.return_value = "2025-01-01T00:00:00Z"

        with patch("paper_search.agent.celery_tasks._get_db", return_value=mock_db):
            result = subscription_check_task()

        assert result["checked"] == 0
        assert result["new_papers"] == 0

    def test_subscription_with_new_papers(self):
        """新论文检测并存储。"""
        from paper_search.agent.celery_tasks import subscription_check_task

        mock_db = MagicMock()
        mock_db.list_subscriptions.return_value = [{
            "id": "sub-001",
            "name": "Test Sub",
            "keywords": "ML",
            "sources": ["arxiv"],
            "interval_hours": 24,
            "last_paper_ids": [],
            "enabled": 1,
        }]
        mock_db._now.return_value = "2025-01-01T00:00:00Z"

        mock_paper = MagicMock()
        mock_paper.title = "New Paper"
        mock_paper.year = 2025
        mock_paper.abstract = "Abstract"
        mock_paper.authors = ["Author"]
        mock_paper.venue = "Venue"
        mock_paper.doi = ""
        mock_paper.source_url = ""
        mock_paper.source = MagicMock()
        mock_paper.source.value = "arxiv"

        mock_result = MagicMock()
        mock_result.papers = [mock_paper]

        with patch("paper_search.agent.celery_tasks._get_db", return_value=mock_db):
            with patch("paper_search.engine.PaperSearchEngine") as mock_eng_cls:
                mock_eng = MagicMock()
                mock_eng_cls.return_value = mock_eng
                with patch("asyncio.new_event_loop") as mock_new_loop:
                    mock_loop = MagicMock()
                    mock_loop.run_until_complete.return_value = mock_result
                    mock_new_loop.return_value = mock_loop

                    result = subscription_check_task()

        assert result["checked"] == 1
        assert result["new_papers"] == 1
        mock_db.save_subscription_result.assert_called()
        mock_db.update_subscription.assert_called()


# ═══════════════════════════════════════════════════════════════
# Test 5: PipelineRunner.run_pipeline_via_celery
# ═══════════════════════════════════════════════════════════════


class TestRunPipelineViaCelery:
    """PipelineRunner.run_pipeline_via_celery 测试。"""

    def test_dispatch_returns_task_id(self):
        """分发返回 celery_task_id。"""
        from paper_search.agent.sub_agent import PipelineRunner

        mock_result = MagicMock()
        mock_result.id = "celery-task-abc123"

        with patch("paper_search.agent.celery_tasks.sub_agent_task") as mock_task:
            mock_task.delay.return_value = mock_result
            result = PipelineRunner.run_pipeline_via_celery(user_query="test")
            assert result["celery_task_id"] == "celery-task-abc123"
            assert result["status"] == "dispatched"
