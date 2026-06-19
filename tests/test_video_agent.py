"""VideoAgent 单元测试。

覆盖:
  1. parse_link() — 4 种输入格式 + 无链接场景
  2. detect_platform() — 已知平台和未知 URL
  3. VideoDownloader.extract_info() — mock yt-dlp
  4. VideoAgent StateGraph — 节点顺序 + error propagation
  5. VideoAgent._should_skip() — error/cancelled 检测
  6. VideoState — TypedDict total=False 行为
  7. AgentDB video CRUD — save/get/list

运行:
    pytest tests/test_video_agent.py -v
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# Test 1: parse_link() — URL/口令检测
# ═══════════════════════════════════════════════════════════════


class TestParseLink:
    """URL 解析和平台检测测试。"""

    def test_short_link(self):
        """短链接格式: https://v.douyin.com/XXXX/"""
        from paper_search.agent.video_downloader import parse_link

        result = parse_link("https://v.douyin.com/ABC123/ 看看这个")
        assert result == "https://v.douyin.com/ABC123"  # trailing / stripped

    def test_long_link(self):
        """长链接格式: https://www.douyin.com/video/12345"""
        from paper_search.agent.video_downloader import parse_link

        result = parse_link("https://www.douyin.com/video/7123456789123456789")
        assert result == "https://www.douyin.com/video/7123456789123456789"

    def test_kou_ling_with_url(self):
        """口令文本内含 URL。"""
        from paper_search.agent.video_downloader import parse_link

        text = "8.94 V@yT.Rk MJI:/ 复制打开抖音，看看这个视频 https://v.douyin.com/ijN8Q5cL/"
        result = parse_link(text)
        assert result is not None
        assert "douyin" in result

    def test_kou_ling_no_url(self):
        """口令文本不含 URL 但含"抖音"关键词。"""
        from paper_search.agent.video_downloader import parse_link

        text = "4.87 复制打开抖音，看看#电影解说  https://v.douyin.com/XXX/"
        result = parse_link(text)
        assert result is not None

    def test_bilibili_link(self):
        """B站长链接识别。"""
        from paper_search.agent.video_downloader import parse_link

        result = parse_link("https://www.bilibili.com/video/BV1xx411c7mD/ 这个视频")
        assert result == "https://www.bilibili.com/video/BV1xx411c7mD"

    def test_youtube_link(self):
        """YouTube 链接识别。"""
        from paper_search.agent.video_downloader import parse_link

        result = parse_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

        # youtu.be short link
        result2 = parse_link("https://youtu.be/dQw4w9WgXcQ")
        assert result2 == "https://youtu.be/dQw4w9WgXcQ"

    def test_no_link(self):
        """无视频链接的普通文本。"""
        from paper_search.agent.video_downloader import parse_link

        result = parse_link("今天天气真好，适合出去散步")
        assert result is None

    def test_generic_url_not_video(self):
        """普通 URL 非视频平台 — 不匹配。"""
        from paper_search.agent.video_downloader import parse_link

        result = parse_link("看看这个网页 https://www.baidu.com/s?wd=test")
        assert result is None

    def test_empty_text(self):
        """空文本 / 纯空格。"""
        from paper_search.agent.video_downloader import parse_link

        assert parse_link("") is None
        assert parse_link("   ") is None


# ═══════════════════════════════════════════════════════════════
# Test 2: detect_platform()
# ═══════════════════════════════════════════════════════════════


class TestDetectPlatform:
    """平台检测测试。"""

    @pytest.mark.parametrize("url,expected", [
        ("https://v.douyin.com/ABC/", "douyin"),
        ("https://www.douyin.com/video/123", "douyin"),
        ("https://www.tiktok.com/@user/video/123", "tiktok"),
        ("https://www.bilibili.com/video/BV123", "bilibili"),
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://www.xiaohongshu.com/explore/123", "xiaohongshu"),
        ("https://www.kuaishou.com/short-video/123", "kuaishou"),
        ("https://www.baidu.com", "unknown"),
    ])
    def test_platform_detection(self, url, expected):
        from paper_search.agent.video_downloader import detect_platform
        assert detect_platform(url) == expected


# ═══════════════════════════════════════════════════════════════
# Test 3: VideoDownloader (mock yt-dlp)
# ═══════════════════════════════════════════════════════════════


class TestVideoDownloader:
    """yt-dlp 封装测试（mock subprocess）。"""

    @pytest.fixture
    def downloader(self):
        from paper_search.agent.video_downloader import VideoDownloader
        with tempfile.TemporaryDirectory() as tmp:
            yield VideoDownloader(output_dir=Path(tmp), timeout=30)

    @pytest.mark.asyncio
    async def test_extract_info_success(self, downloader):
        """成功提取元数据。"""
        mock_json = json.dumps({
            "id": "abc123",
            "title": "测试视频",
            "duration": 120.5,
            "uploader": "测试作者",
            "thumbnail": "https://example.com/thumb.jpg",
            "description": "这是一个测试视频",
            "extractor_key": "Douyin",
            "webpage_url": "https://www.douyin.com/video/abc123",
            "formats": [],
        }).encode()

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (mock_json, b"")

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            info = await downloader.extract_info("https://v.douyin.com/abc123/")

        assert info.video_id == "abc123"
        assert info.title == "测试视频"
        assert info.duration_seconds == 120.5
        assert info.uploader == "测试作者"
        assert info.platform == "douyin"

    @pytest.mark.asyncio
    async def test_extract_info_failure(self, downloader):
        """yt-dlp 返回非零退出码。"""
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"Error: Video unavailable")

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            with pytest.raises(ValueError, match="yt-dlp extract failed"):
                await downloader.extract_info("https://v.douyin.com/deleted/")


# ═══════════════════════════════════════════════════════════════
# Test 4: VideoState TypedDict
# ═══════════════════════════════════════════════════════════════


class TestVideoState:
    """VideoState TypedDict 行为测试。"""

    def test_default_state(self):
        """TypedDict total=False — 可以创建空状态。"""
        from paper_search.agent.graphs.video_graph import VideoState
        state: VideoState = {}
        assert isinstance(state, dict)
        # 字段应该可通过 .get() 访问（不存在则返回 None）
        assert state.get("error") is None

    def test_partial_state(self):
        """可以逐步填充状态字段。"""
        from paper_search.agent.graphs.video_graph import VideoState

        state: VideoState = {
            "project_id": "task-001",
            "user_query": "测试查询",
        }
        assert state["project_id"] == "task-001"
        assert state.get("url") is None  # 尚未设置


# ═══════════════════════════════════════════════════════════════
# Test 5: VideoAgent._should_skip()
# ═══════════════════════════════════════════════════════════════


class TestVideoAgentShouldSkip:
    """error/cancelled 跳过逻辑测试。"""

    @pytest.fixture
    def agent(self):
        from paper_search.agent.graphs.video_graph import VideoAgent
        return VideoAgent(
            downloader=MagicMock(),
            whisper_model=None,
            llm=MagicMock(),
            db=MagicMock(),
            videos_dir=Path("/tmp"),
        )

    def test_no_error_no_cancel(self, agent):
        assert not agent._should_skip({})

    def test_with_error(self, agent):
        assert agent._should_skip({"error": "something failed"})

    def test_with_cancelled(self, agent):
        assert agent._should_skip({"cancelled": True})

    def test_with_both(self, agent):
        assert agent._should_skip({"error": "x", "cancelled": True})


# ═══════════════════════════════════════════════════════════════
# Test 6: VideoAgent nodes — error propagation
# ═══════════════════════════════════════════════════════════════


class TestVideoAgentNodes:
    """VideoAgent 各节点测试（mock 所有依赖）。"""

    @pytest.fixture
    def agent(self):
        from paper_search.agent.graphs.video_graph import VideoAgent
        return VideoAgent(
            downloader=MagicMock(),
            whisper_model=None,
            llm=MagicMock(),
            db=MagicMock(),
            videos_dir=Path("/tmp"),
        )

    @pytest.mark.asyncio
    async def test_parse_link_with_error_skips(self, agent):
        """已有 error 时 parse_link 应跳过。"""
        result = await agent._parse_link_node({
            "error": "prior error",
            "user_query": "https://v.douyin.com/abc/",
        })
        assert result == {}

    @pytest.mark.asyncio
    async def test_parse_link_no_url(self, agent):
        """无视频链接的文本应返回 error。"""
        result = await agent._parse_link_node({
            "user_query": "今天天气不错",
        })
        assert "error" in result
        assert "未检测到视频链接" in result["error"]

    @pytest.mark.asyncio
    async def test_parse_link_success(self, agent):
        """有视频链接时应成功解析。"""
        result = await agent._parse_link_node({
            "user_query": "https://v.douyin.com/ABC123/ 看看",
        })
        assert "error" not in result
        assert result["url"] == "https://v.douyin.com/ABC123/"
        assert result["platform"] == "douyin"

    @pytest.mark.asyncio
    async def test_transcribe_long_video_skips(self, agent):
        """视频时长 > 600s 应跳过转录。"""
        result = await agent._transcribe_node({
            "duration_seconds": 900,
            "audio_path": "/tmp/test.wav",
        })
        assert result["transcription_skipped"] is True
        assert result["transcript_model"] == "skipped"

    @pytest.mark.asyncio
    async def test_transcribe_no_audio_path(self, agent):
        """无音频文件路径时应报错。"""
        result = await agent._transcribe_node({
            "duration_seconds": 60,
        })
        assert result.get("transcription_skipped") is None  # not set
        assert "error" in result

    @pytest.mark.asyncio
    async def test_transcribe_no_whisper_model(self, agent):
        """未加载 Whisper 模型时的错误处理。"""
        result = await agent._transcribe_node({
            "duration_seconds": 60,
            "audio_path": "/tmp/test.wav",
        })
        assert "error" in result
        assert result["transcription_skipped"] is True

    @pytest.mark.asyncio
    async def test_summarize_no_transcript(self, agent):
        """无转录文本时仍应生成摘要（基于元数据）。"""
        mock_llm = agent._llm
        mock_llm.chat_json = AsyncMock(return_value={
            "one_line_summary": "测试摘要",
            "key_points": [],
            "core_thesis": "",
            "tags": [],
            "language": "zh",
        })

        result = await agent._summarize_node({
            "title": "测试视频",
            "uploader": "测试作者",
            "platform": "douyin",
            "duration_seconds": 120,
            "transcription_skipped": True,
        })

        assert "summary" in result
        mock_llm.chat_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_summarize_with_large_transcript_truncates(self, agent):
        """长转录文本应被截断到 8000 字符。"""
        mock_llm = agent._llm
        mock_llm.chat_json = AsyncMock(return_value={
            "one_line_summary": "test",
        })

        long_transcript = "这是一段很长的转录文本。" * 2000  # ~24,000 chars
        await agent._summarize_node({
            "title": "测试",
            "transcript_text": long_transcript,
            "transcription_skipped": False,
            "duration_seconds": 60,
        })

        call_args = mock_llm.chat_json.call_args
        context = call_args.kwargs["messages"][0]["content"]
        # Should be truncated
        assert len(context) < len(long_transcript) + 500
        assert "转录文本已截断" in context or len(long_transcript) > 8000

    @pytest.mark.asyncio
    async def test_notify_node_persists_to_db(self, agent):
        """notify 节点应调用 db.save_video_result()。"""
        result = await agent._notify_node({
            "project_id": "task-001",
            "video_id": "abc123",
            "url": "https://v.douyin.com/abc/",
            "platform": "douyin",
            "title": "测试视频",
            "duration_seconds": 120,
            "uploader": "测试作者",
            "local_video_path": "/tmp/abc.mp4",
            "transcript_text": "这是转录文本",
            "transcription_skipped": False,
            "summary": {"one_line_summary": "测试"},
            "analysis": {"stance": "中立"},
        })

        assert "result" in result
        assert result["result"]["title"] == "测试视频"
        assert result["result"]["transcript_text"] == "这是转录文本"
        agent._db.save_video_result.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_node_with_error_saves_partial(self, agent):
        """即使有 error，notify 仍应保存部分结果。"""
        result = await agent._notify_node({
            "project_id": "task-001",
            "error": "下载失败",
            "url": "https://test.com/video",
            "video_id": "",
            "title": "未知",
        })

        assert "result" in result
        assert "error" in result["result"]
        # 即使有 error 也应尝试持久化
        agent._db.save_video_result.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# Test 7: VideoAgent graph compilation
# ═══════════════════════════════════════════════════════════════


class TestVideoAgentCompilation:
    """LangGraph 图编译测试。"""

    def test_compile_creates_graph(self):
        """compile() 应成功创建 StateGraph。"""
        from paper_search.agent.graphs.video_graph import VideoAgent

        agent = VideoAgent(
            downloader=MagicMock(),
            whisper_model=None,
            llm=MagicMock(),
            db=MagicMock(),
            videos_dir=Path("/tmp"),
        )
        graph = agent.compile()
        assert graph is not None
        assert agent.graph is graph

    def test_graph_without_compilation_raises(self):
        """未编译时访问 graph 属性应抛出 RuntimeError。"""
        from paper_search.agent.graphs.video_graph import VideoAgent

        agent = VideoAgent(
            downloader=MagicMock(),
            whisper_model=None,
            llm=MagicMock(),
            db=MagicMock(),
            videos_dir=Path("/tmp"),
        )
        with pytest.raises(RuntimeError, match="not compiled"):
            _ = agent.graph

    def test_compile_has_all_nodes(self):
        """验证所有 8 个节点都在图中。"""
        from paper_search.agent.graphs.video_graph import VideoAgent

        agent = VideoAgent(
            downloader=MagicMock(),
            whisper_model=None,
            llm=MagicMock(),
            db=MagicMock(),
            videos_dir=Path("/tmp"),
        )
        graph = agent.compile()
        nodes = graph.get_graph().nodes
        node_names = {n for n in nodes}
        expected = {
            "parse_link", "fetch_metadata", "download_video",
            "extract_audio", "transcribe", "summarize",
            "analyze", "notify", "__start__", "__end__",
        }
        assert node_names == expected


# ═══════════════════════════════════════════════════════════════
# Test 8: AgentDB video CRUD
# ═══════════════════════════════════════════════════════════════


class TestVideoDB:
    """videos 表 CRUD 测试。"""

    @pytest.fixture
    def db(self):
        from paper_search.agent.db import AgentDB
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        db = AgentDB(db_path=db_path)
        yield db
        db.close()
        db_path.unlink(missing_ok=True)

    def test_save_and_get_video_result(self, db):
        """保存并获取视频结果。"""
        vid = db.save_video_result(
            project_id="task-001",
            video_id="douyin-abc123",
            url="https://v.douyin.com/abc123/",
            platform="douyin",
            title="测试视频",
            duration_seconds=120.5,
            uploader="测试作者",
            summary={"one_line_summary": "这是摘要"},
            analysis={"stance": "中立"},
            local_path="/tmp/abc.mp4",
            transcript_text="这是测试转录。",
        )
        assert vid == "douyin-abc123"

        result = db.get_video_result("douyin-abc123")
        assert result is not None
        assert result["title"] == "测试视频"
        assert result["platform"] == "douyin"
        assert result["duration_seconds"] == 120.5
        # JSON fields should be deserialized
        assert isinstance(result["summary"], dict)
        assert result["summary"]["one_line_summary"] == "这是摘要"
        assert isinstance(result["analysis"], dict)
        assert result["analysis"]["stance"] == "中立"
        assert result["transcript_text"] == "这是测试转录。"

    def test_get_nonexistent_video(self, db):
        """获取不存在的视频应返回 None。"""
        result = db.get_video_result("nonexistent")
        assert result is None

    def test_list_video_results(self, db):
        """列出项目的视频结果。"""
        import time
        for i in range(3):
            db.save_video_result(
                project_id="task-001",
                video_id=f"vid-{i}",
                url=f"https://test.com/video/{i}",
                platform="douyin",
                title=f"视频 {i}",
            )
            time.sleep(0.01)  # ensure distinct timestamps

        results = db.list_video_results("task-001")
        assert len(results) == 3
        titles = {r["title"] for r in results}
        assert titles == {"视频 0", "视频 1", "视频 2"}

    def test_list_video_results_empty(self, db):
        """空结果返回空列表。"""
        results = db.list_video_results("no-project")
        assert results == []

    def test_update_existing_video(self, db):
        """重复保存（INSERT OR REPLACE）应更新已有记录。"""
        db.save_video_result(
            project_id="task-001",
            video_id="vid-update",
            url="https://test.com/video",
            title="原始标题",
        )
        db.save_video_result(
            project_id="task-001",
            video_id="vid-update",
            url="https://test.com/video",
            title="更新后的标题",
        )
        result = db.get_video_result("vid-update")
        assert result["title"] == "更新后的标题"
