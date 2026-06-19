"""VideoAgent — 视频解析子 Agent。

8 节点线性 StateGraph:
  parse_link → fetch_metadata → download_video → extract_audio
  → transcribe → summarize → analyze → notify

功能:
  - 解析抖音/TikTok 等视频分享链接 (短链/长链/口令)
  - yt-dlp 下载视频 + ffmpeg 提取音频
  - faster-whisper 本地语音识别
  - LLM 结构化摘要 + 深度分析
  - 结果持久化到 SQLite + 文件系统

长视频 (>10分钟): 跳过转录，仅基于元数据生成摘要。

用法:
    from .video_graph import VideoAgent

    agent = VideoAgent(downloader, whisper_model, llm, db, videos_dir)
    graph = agent.compile()
    result = await graph.ainvoke({
        "project_id": "task-xxx",
        "user_query": "https://v.douyin.com/XXXX/ 看看这个视频",
    })
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# LLM Prompt Templates
# ═══════════════════════════════════════════════════════════════

VIDEO_SUMMARIZE_PROMPT = """你是一个视频内容分析专家。给定视频信息（标题、上传者、平台、简介、转录全文），
输出结构化摘要。

输出纯 JSON（不要 markdown 代码块）:
{
  "one_line_summary": "一句话总结视频核心内容",
  "key_points": [
    {"title": "要点标题", "content": "详细说明"},
    {"title": "要点标题", "content": "详细说明"}
  ],
  "core_thesis": "视频的核心论点或主张",
  "tags": ["标签1", "标签2", "标签3"],
  "language": "视频主要语言 (zh/en)"
}

要求:
- key_points 列出 3-5 个主要观点
- tags 使用中英双语标签
- 如果没有转录文本，基于标题和简介合理推断
- 保持客观，不添加视频中未提及的内容
- 如果无法判断语言，默认使用 zh"""

VIDEO_ANALYSIS_PROMPT = """你是一个媒体内容分析专家。给定视频信息（标题、摘要、转录全文），
输出深度分析。

输出纯 JSON（不要 markdown 代码块）:
{
  "stance": "视频的立场 (中立/赞成/反对/批判/宣传)",
  "stance_confidence": 0.85,
  "logic_chain": [
    {"premise": "前提1", "conclusion": "结论1"},
    {"premise": "前提2", "conclusion": "结论2"}
  ],
  "factual_claims": [
    {
      "claim": "视频中的具体陈述",
      "verdict": "supported/unsupported/unverifiable",
      "evidence": "判断依据",
      "confidence": 0.9
    }
  ],
  "overall_assessment": "总体评价（客观公正）",
  "target_audience": "目标受众分析",
  "production_quality": "制作质量评估 (high/medium/low)"
}

要求:
- stance_confidence: 0.0~1.0，不能确定时取 0.3~0.5
- logic_chain: 提取 2-4 个逻辑链路
- factual_claims: 列出 0-5 个可验证的陈述
- verdict: "supported"=有证据支持, "unsupported"=明显可疑, "unverifiable"=无法验证
- 如果视频被跳过转录（超过10分钟），基于摘要合理推断
- 没有足够信息时，如实标注 "insufficient_info"
- 保持批判性思维，不要盲目相信视频内容"""

# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class VideoState(TypedDict, total=False):
    """VideoAgent 状态定义 — 所有字段都可选（total=False），
    由各节点逐步填充。"""

    # ── Input ──
    project_id: str                             # 主任务 ID
    user_query: str                             # 用户原始消息
    url: str                                    # 解析后的规范 URL
    platform: str                               # "douyin" | "tiktok" | ...

    # ── Metadata (fetched by _fetch_metadata_node) ──
    video_id: str
    title: str
    duration_seconds: float
    uploader: str
    thumbnail_url: str
    description: str

    # ── Local paths ──
    local_video_path: str                       # 下载的视频文件路径
    audio_path: str                             # 提取的音频文件路径

    # ── Transcription ──
    transcript_text: str                        # 完整转录文本
    transcript_model: str                       # 使用的模型名
    transcription_skipped: bool                 # 是否因长视频跳过

    # ── LLM Outputs ──
    summary: dict                               # 结构化摘要
    analysis: dict                              # 深度分析

    # ── Control ──
    cancelled: bool                             # 取消标志

    # ── Output ──
    result: dict                                # 最终结果
    error: str                                  # 错误信息


# ═══════════════════════════════════════════════════════════════
# VideoAgent
# ═══════════════════════════════════════════════════════════════


class VideoAgent:
    """视频解析子 Agent — 8 节点线性 StateGraph。

    依赖:
      - downloader: VideoDownloader (yt-dlp 封装)
      - whisper_model: faster-whisper model 实例
      - llm: LLMClientV2 实例
      - db: AgentDB 实例
      - videos_dir: 视频存储目录
    """

    TOTAL_STAGES = 8

    def __init__(
        self,
        downloader,                      # VideoDownloader
        whisper_model: Any,              # faster_whisper.WhisperModel
        llm,                             # LLMClientV2
        db,                              # AgentDB
        videos_dir: Path,
        on_progress=None,
    ):
        self._downloader = downloader
        self._whisper = whisper_model
        self._llm = llm
        self._db = db
        self._videos_dir = videos_dir
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        """Build 8-node linear StateGraph."""
        builder = StateGraph(VideoState)

        builder.add_node("parse_link", self._parse_link_node)
        builder.add_node("fetch_metadata", self._fetch_metadata_node)
        builder.add_node("download_video", self._download_video_node)
        builder.add_node("extract_audio", self._extract_audio_node)
        builder.add_node("transcribe", self._transcribe_node)
        builder.add_node("summarize", self._summarize_node)
        builder.add_node("analyze", self._analyze_node)
        builder.add_node("notify", self._notify_node)

        # Linear chain
        builder.add_edge(START, "parse_link")
        builder.add_edge("parse_link", "fetch_metadata")
        builder.add_edge("fetch_metadata", "download_video")
        builder.add_edge("download_video", "extract_audio")
        builder.add_edge("extract_audio", "transcribe")
        builder.add_edge("transcribe", "summarize")
        builder.add_edge("summarize", "analyze")
        builder.add_edge("analyze", "notify")
        builder.add_edge("notify", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("VideoAgent not compiled — call compile() first")
        return self._graph

    # ── Helper ───────────────────────────────────────────

    def _should_skip(self, state: VideoState) -> bool:
        """Check if the current node should be skipped due to prior error or cancellation."""
        if state.get("error"):
            logger.warning(f"    VideoAgent: skipping due to prior error: {state['error'][:100]}")
            return True
        if state.get("cancelled"):
            logger.warning("    VideoAgent: skipping due to cancellation")
            return True
        return False

    async def _notify(self, stage: str, index: int, total: int, message: str):
        """Notify progress via on_progress callback."""
        logger.info(f"    VideoAgent [{index}/{total}] {stage}: {message}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, 0, 0)
            except Exception as e:
                logger.debug(f"VideoAgent on_progress error: {e}")

    # ── Node 1: parse_link ───────────────────────────────

    async def _parse_link_node(self, state: VideoState) -> dict:
        """Node 1: Parse URL from user text, detect platform."""
        if self._should_skip(state):
            return {}

        text = state.get("user_query", "")
        await self._notify("解析链接", 1, self.TOTAL_STAGES,
                          f"解析视频链接: {text[:60]}")

        from ..video_downloader import parse_link, detect_platform

        url = parse_link(text)
        if not url:
            msg = f"未检测到视频链接: {text[:100]}"
            logger.warning(msg)
            return {
                "error": msg,
                "result": {"error": "no_video_link_detected", "user_text": text},
            }

        platform = detect_platform(url)
        return {
            "url": url,
            "platform": platform,
        }

    # ── Node 2: fetch_metadata ───────────────────────────

    async def _fetch_metadata_node(self, state: VideoState) -> dict:
        """Node 2: Extract video metadata via yt-dlp --dump-json."""
        if self._should_skip(state):
            return {}

        url = state.get("url", "")
        await self._notify("获取元数据", 2, self.TOTAL_STAGES,
                          f"获取视频信息: {url[:60]}")

        try:
            info = await self._downloader.extract_info(url)
            return {
                "video_id": info.video_id,
                "title": info.title,
                "duration_seconds": info.duration_seconds,
                "uploader": info.uploader,
                "thumbnail_url": info.thumbnail_url,
                "description": info.description,
                "platform": info.platform or state.get("platform", ""),
            }
        except Exception as e:
            error_msg = f"视频信息获取失败: {e}"
            logger.error(error_msg, exc_info=True)
            return {"error": error_msg}

    # ── Node 3: download_video ───────────────────────────

    async def _download_video_node(self, state: VideoState) -> dict:
        """Node 3: Download video via yt-dlp."""
        if self._should_skip(state):
            return {}

        url = state.get("url", "")
        title = state.get("title", "<未知>")
        await self._notify("下载视频", 3, self.TOTAL_STAGES,
                          f"下载视频: {title[:40]}")

        try:
            video_path = await self._downloader.download_video(url)
            return {
                "local_video_path": str(video_path),
            }
        except Exception as e:
            error_msg = f"视频下载失败: {e}"
            logger.error(error_msg, exc_info=True)
            return {"error": error_msg}

    # ── Node 4: extract_audio ────────────────────────────

    async def _extract_audio_node(self, state: VideoState) -> dict:
        """Node 4: Extract audio from video via ffmpeg."""
        if self._should_skip(state):
            return {}

        video_path_str = state.get("local_video_path", "")
        if not video_path_str:
            return {"error": "No video file to extract audio from"}

        video_path = Path(video_path_str)
        await self._notify("提取音频", 4, self.TOTAL_STAGES,
                          "提取音频流 (ffmpeg)")

        try:
            audio_path = self._downloader.extract_audio(video_path)
            return {
                "audio_path": str(audio_path),
            }
        except Exception as e:
            error_msg = f"音频提取失败: {e}"
            logger.error(error_msg, exc_info=True)
            return {"error": error_msg}

    # ── Node 5: transcribe ───────────────────────────────

    async def _transcribe_node(self, state: VideoState) -> dict:
        """Node 5: Transcribe audio via faster-whisper.

        Skip if video > 10 minutes (600 seconds).
        """
        if self._should_skip(state):
            return {}

        duration = state.get("duration_seconds", 0)
        audio_path_str = state.get("audio_path", "")

        # Long video check — > 10 minutes
        if duration > 600:
            mins = int(duration // 60)
            secs = int(duration % 60)
            await self._notify("语音识别", 5, self.TOTAL_STAGES,
                              f"视频时长 {mins}m{secs}s > 10分钟，跳过转录")
            return {
                "transcript_text": "",
                "transcript_model": "skipped",
                "transcription_skipped": True,
            }

        if not audio_path_str:
            return {"error": "No audio file for transcription"}

        audio_path = Path(audio_path_str)
        if not audio_path.exists():
            return {"error": f"Audio file not found: {audio_path_str}"}

        await self._notify("语音识别", 5, self.TOTAL_STAGES,
                          f"转录中 (时长: {duration:.0f}秒)")

        if self._whisper is None:
            return {
                "error": "Whisper model not loaded — check faster-whisper installation",
                "transcription_skipped": True,
            }

        try:
            segments, info = self._whisper.transcribe(
                str(audio_path),
                beam_size=5,
                language=None,          # auto-detect (Chinese/English)
                vad_filter=True,        # filter out non-speech
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                ),
            )
            transcript_parts = []
            for seg in segments:
                transcript_parts.append(f"[{seg.start:.1f}-{seg.end:.1f}] {seg.text.strip()}")
            transcript_text = "\n".join(transcript_parts)

            detected_lang = getattr(info, "language", "unknown")
            logger.info(
                f"    Transcription done: {len(transcript_parts)} segments, "
                f"language={detected_lang}, {len(transcript_text)} chars"
            )

            return {
                "transcript_text": transcript_text,
                "transcript_model": f"faster-whisper-{detected_lang}",
                "transcription_skipped": False,
            }
        except Exception as e:
            error_msg = f"语音识别失败: {e}"
            logger.error(error_msg, exc_info=True)
            return {"error": error_msg, "transcription_skipped": True}

    # ── Node 6: summarize ────────────────────────────────

    async def _summarize_node(self, state: VideoState) -> dict:
        """Node 6: LLM structured summary from transcript or metadata.

        For long videos (transcription skipped), uses title + description only.
        For normal videos, uses full transcript (truncated to 8000 chars).
        """
        if self._should_skip(state):
            return {}

        title = state.get("title", "未命名视频")
        uploader = state.get("uploader", "")
        platform = state.get("platform", "unknown")
        description = state.get("description", "")
        transcript = state.get("transcript_text")
        is_skipped = state.get("transcription_skipped", False)
        duration = state.get("duration_seconds", 0)
        duration_str = f"{int(duration // 60)}分{int(duration % 60)}秒"

        await self._notify("生成摘要", 6, self.TOTAL_STAGES,
                          "LLM 结构化摘要生成中")

        # ── Build context ──
        if is_skipped or not transcript:
            context = (
                f"视频标题: {title}\n"
                f"上传者: {uploader}\n"
                f"平台: {platform}\n"
                f"时长: {duration_str}\n"
                f"简介: {description}\n\n"
                f"注意: 视频时长超过10分钟或转录不可用，未获取完整转录文本。"
                f"请基于标题和简介生成摘要。"
            )
        else:
            max_chars = 8000
            truncated = transcript[:max_chars]
            if len(transcript) > max_chars:
                truncated += "\n\n... [转录文本已截断]"
            context = (
                f"视频标题: {title}\n"
                f"上传者: {uploader}\n"
                f"平台: {platform}\n"
                f"时长: {duration_str}\n"
                f"简介: {description}\n\n"
                f"转录全文:\n{truncated}"
            )

        try:
            summary = await self._llm.chat_json(
                messages=[{"role": "user", "content": context}],
                system=VIDEO_SUMMARIZE_PROMPT,
            )
            # chat_json returns a dict — ensure it has expected keys
            if not isinstance(summary, dict):
                summary = {"one_line_summary": str(summary)}
            logger.info(f"    Summary: {summary.get('one_line_summary', 'N/A')[:80]}")
            return {"summary": summary}
        except Exception as e:
            error_msg = f"摘要生成失败: {e}"
            logger.error(error_msg, exc_info=True)
            # Non-fatal — continue with partial result
            return {
                "summary": {
                    "one_line_summary": title,
                    "key_points": [],
                    "core_thesis": "",
                    "tags": [],
                    "language": "zh",
                    "llm_error": error_msg,
                },
            }

    # ── Node 7: analyze ──────────────────────────────────

    async def _analyze_node(self, state: VideoState) -> dict:
        """Node 7: LLM deep analysis — stance, logic chain, fact-checking.

        Only runs if we have a transcript or at least a summary from the previous node.
        """
        if self._should_skip(state):
            return {}

        title = state.get("title", "")
        transcript = state.get("transcript_text")
        is_skipped = state.get("transcription_skipped", False)
        summary = state.get("summary", {})

        await self._notify("深度分析", 7, self.TOTAL_STAGES,
                          "LLM 深度分析中")

        if not transcript and not is_skipped:
            # Transcription failed silently — still try with summary
            logger.warning("No transcript available, attempting analysis from summary only")

        # ── Build context ──
        context_parts = [f"视频标题: {title}"]
        if summary:
            context_parts.append(
                f"摘要: {json.dumps(summary, ensure_ascii=False)}"
            )
        if transcript and not is_skipped:
            max_chars = 6000
            truncated = transcript[:max_chars]
            if len(transcript) > max_chars:
                truncated += "\n\n... [转录文本已截断]"
            context_parts.append(f"转录全文:\n{truncated}")
        elif is_skipped:
            context_parts.append("注意: 视频时长超过10分钟，转录已被跳过。请基于标题和摘要进行分析。")

        context = "\n\n".join(context_parts)

        try:
            analysis = await self._llm.chat_json(
                messages=[{"role": "user", "content": context}],
                system=VIDEO_ANALYSIS_PROMPT,
            )
            if not isinstance(analysis, dict):
                analysis = {"overall_assessment": str(analysis)}
            logger.info(
                f"    Analysis: stance={analysis.get('stance', '?')}, "
                f"quality={analysis.get('production_quality', '?')}"
            )
            return {"analysis": analysis}
        except Exception as e:
            error_msg = f"深度分析失败: {e}"
            logger.error(error_msg, exc_info=True)
            return {
                "analysis": {
                    "error": error_msg,
                    "overall_assessment": f"分析由于 LLM 错误而未能完成: {e}",
                },
            }

    # ── Node 8: notify ───────────────────────────────────

    async def _notify_node(self, state: VideoState) -> dict:
        """Node 8: Persist results and return final result dict."""
        error = state.get("error", "")

        if error:
            await self._notify("完成通知", 8, self.TOTAL_STAGES,
                              f"视频处理结束 (有错误): {error[:60]}")
        else:
            await self._notify("完成通知", 8, self.TOTAL_STAGES,
                              "视频处理完成，保存结果")

        # ── Read transcript (even if skipped) ──
        transcript_text = state.get("transcript_text") or ""

        # ── Build result ──
        result = {
            "url": state.get("url", ""),
            "platform": state.get("platform", ""),
            "video_id": state.get("video_id", ""),
            "title": state.get("title", ""),
            "duration_seconds": state.get("duration_seconds", 0),
            "uploader": state.get("uploader", ""),
            "local_video_path": state.get("local_video_path", ""),
            "transcript_text": transcript_text if not state.get("transcription_skipped") else None,
            "transcription_skipped": state.get("transcription_skipped", False),
            "summary": state.get("summary"),
            "analysis": state.get("analysis"),
        }

        if error:
            result["error"] = error

        # ── Persist to SQLite ──
        try:
            self._db.save_video_result(
                project_id=state.get("project_id", ""),
                video_id=state.get("video_id", ""),
                url=state.get("url", ""),
                platform=state.get("platform", ""),
                title=state.get("title", ""),
                duration_seconds=state.get("duration_seconds", 0),
                uploader=state.get("uploader", ""),
                summary=state.get("summary"),
                analysis=state.get("analysis"),
                local_path=state.get("local_video_path", ""),
                transcript_text=transcript_text if not state.get("transcription_skipped") else None,
            )
        except Exception as e:
            logger.warning(f"Failed to save video result to DB: {e}")

        # ── Write transcript to file ──
        if transcript_text and not state.get("transcription_skipped"):
            try:
                vid = state.get("video_id", "unknown")
                transcript_path = self._videos_dir / f"{vid}_transcript.txt"
                transcript_path.write_text(transcript_text, encoding="utf-8")
                result["transcript_path"] = str(transcript_path)
                logger.info(f"Transcript saved: {transcript_path}")
            except Exception as e:
                logger.warning(f"Failed to write transcript file: {e}")

        # ── Clean up audio file (keep video) ──
        audio_path_str = state.get("audio_path", "")
        if audio_path_str:
            try:
                Path(audio_path_str).unlink(missing_ok=True)
                logger.debug(f"Cleaned up audio: {audio_path_str}")
            except Exception as e:
                logger.debug(f"Failed to clean up audio: {e}")

        return {
            "result": result,
        }
