"""Celery Task 单元测试。

覆盖:
   1. subscription_check_task — mock engine + DB

运行:
    PYTHONPATH=src pytest tests/test_celery_tasks.py -v
"""

from unittest.mock import MagicMock, patch


# ═══════════════════════════════════════════════════════════════
# subscription_check_task
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
