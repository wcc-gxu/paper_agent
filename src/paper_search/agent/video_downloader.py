"""yt-dlp wrapper + video URL/link parser.

Platform-agnostic — works with any site supported by yt-dlp extractors.
Supports 3 link formats for Douyin: short link, long link, and 口令 text.

Usage:
    from .video_downloader import VideoDownloader, parse_link, detect_platform

    url = parse_link("https://v.douyin.com/XXXX/")
    downloader = VideoDownloader(output_dir=Path("/tmp/videos"))
    info = await downloader.extract_info(url)
    path = await downloader.download_video(url)
    audio = downloader.extract_audio(path)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# URL / Link Parsing
# ═══════════════════════════════════════════════════════════════

# Douyin short link: https://v.douyin.com/XXXXX/
SHORT_LINK_RE = re.compile(r"https?://v\.douyin\.com/\w+(/\w*)?")

# Douyin long link: https://www.douyin.com/video/123456789
LONG_LINK_RE = re.compile(r"https?://(www\.)?douyin\.com/video/(\d+)")

# Generic URL extractor
URL_IN_TEXT_RE = re.compile(r"https?://\S+")

# 口令 pattern: starts with "digit.digit <letters>" followed by emoji-like chars
KOU_LING_RE = re.compile(
    r"(\d+\.\d+)\s+[A-Za-z@.]+?\s+[A-Za-z]+[:：]?/",
    re.UNICODE,
)

# Known video platforms for multi-platform support
PLATFORM_PATTERNS: dict[str, re.Pattern] = {
    "douyin": re.compile(r"douyin\.com"),
    "tiktok": re.compile(r"(tiktok\.com|t\.co/)"),
    "bilibili": re.compile(r"bilibili\.com"),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)"),
    "xiaohongshu": re.compile(r"xiaohongshu\.com"),
    "kuaishou": re.compile(r"kuaishou\.com"),
}


def parse_link(text: str) -> Optional[str]:
    """Detect video sharing link from user text.

    Supports 3 formats:
      1. Short link: https://v.douyin.com/XXXX/
      2. Long link: https://www.douyin.com/video/XXXX
      3. 口令/encoded text containing "抖音"

    Args:
        text: User input text that may contain a video link

    Returns:
        Canonical URL string for yt-dlp, or None if no link found.
    """
    # ── 1. Extract any URLs from text ──
    urls = URL_IN_TEXT_RE.findall(text)
    for url in urls:
        # Clean trailing punctuation
        url = url.rstrip("/.,;:!?)")

        # Douyin-specific patterns
        if SHORT_LINK_RE.match(url):
            logger.debug(f"Detected douyin short link: {url}")
            return url
        if LONG_LINK_RE.match(url):
            logger.debug(f"Detected douyin long link: {url}")
            return url

        # Generic: check if it's a known video platform
        for platform, pattern in PLATFORM_PATTERNS.items():
            if pattern.search(url):
                logger.debug(f"Detected {platform} link: {url}")
                return url

    # ── 2. 口令 (copy-paste password) pattern ──
    if "抖音" in text:
        # Try to extract embedded URL from the 口令 text
        if urls:
            logger.debug(f"Detected douyin 口令 with embedded URL: {urls[0]}")
            return urls[0]

        # Check for 口令 pattern
        kou_ling_match = KOU_LING_RE.search(text)
        if kou_ling_match:
            # URL extraction already attempted above; if no URL found,
            # return the full text as yt-dlp may handle it
            logger.debug("Detected douyin 口令 (no embedded URL)")
            # Extract the alphanumeric code portion for yt-dlp
            code_match = re.search(r"([A-Za-z@.]+)\s+([A-Za-z]+[:：]?/?)", text)
            if code_match:
                # Not a standard URL — return None and let caller handle
                pass

    # ── 3. Generic copy-paste text with Chinese video platform hints ──
    chinese_video_hints = ["复制此链接", "打开抖音", "打開抖音", "长按复制", "分享视频",
                           "分享的視頻", "douyin.com", "打开观看"]
    if any(hint in text for hint in chinese_video_hints):
        if urls:
            return urls[0]

    return None


def detect_platform(url: str) -> str:
    """Detect video platform from URL.

    Args:
        url: Canonical video URL

    Returns:
        Platform name string ("douyin", "tiktok", "bilibili", "youtube",
        "xiaohongshu", "kuaishou", or "unknown")
    """
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return platform
    return "unknown"


# ═══════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════


@dataclass
class VideoInfo:
    """Metadata extracted by yt-dlp before download."""
    url: str = ""
    platform: str = ""              # "douyin" | "tiktok" | "youtube" | ...
    video_id: str = ""
    title: str = ""
    duration_seconds: float = 0.0
    uploader: str = ""
    thumbnail_url: str = ""
    description: str = ""
    extractor: str = ""             # yt-dlp extractor key, e.g. "Douyin"
    webpage_url: str = ""
    original_url: str = ""
    formats: list[dict] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# VideoDownloader
# ═══════════════════════════════════════════════════════════════


class VideoDownloader:
    """yt-dlp wrapper for video metadata extraction and download.

    Usage:
        downloader = VideoDownloader(output_dir=Path("~/videos"))
        info = await downloader.extract_info("https://v.douyin.com/XXXX/")
        path = await downloader.download_video("https://v.douyin.com/XXXX/")
        audio = downloader.extract_audio(path)
    """

    def __init__(self, output_dir: Path, timeout: int = 300):
        """Initialize downloader.

        Args:
            output_dir: Directory for downloaded videos and audio files
            timeout: Maximum seconds for yt-dlp operations (default 5 min)
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._current_process: Optional[asyncio.subprocess.Process] = None

    # ── Metadata Extraction ──────────────────────────────

    async def extract_info(self, url: str) -> VideoInfo:
        """Extract video metadata without downloading.

        Runs: yt-dlp --dump-json --no-download URL

        Args:
            url: Video URL

        Returns:
            VideoInfo with title, duration, uploader, etc.

        Raises:
            ValueError: If yt-dlp fails (URL invalid, video deleted, etc.)
            TimeoutError: If yt-dlp takes longer than self.timeout
        """
        cmd = [
            "yt-dlp", "--dump-json", "--no-download",
            "--no-warnings", "--no-playlist",
            url,
        ]
        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._current_process = proc
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )

            if proc.returncode != 0:
                err_text = stderr.decode(errors="replace")[:500]
                raise ValueError(
                    f"yt-dlp extract failed (exit {proc.returncode}): {err_text}"
                )

            data = json.loads(stdout.decode())
            return VideoInfo(
                url=data.get("webpage_url", url),
                platform=detect_platform(url),
                video_id=data.get("id", ""),
                title=data.get("title", ""),
                duration_seconds=float(data.get("duration", 0)),
                uploader=data.get("uploader", data.get("channel", "")),
                thumbnail_url=data.get("thumbnail", ""),
                description=(data.get("description") or "")[:500],
                extractor=data.get("extractor_key", ""),
                webpage_url=data.get("webpage_url", url),
                original_url=url,
                formats=data.get("formats", []),
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"yt-dlp info extraction timed out after {self.timeout}s"
            )
        finally:
            self._current_process = None

    # ── Video Download ────────────────────────────────────

    async def download_video(self, url: str) -> Path:
        """Download video to output_dir.

        Uses best mp4 quality. Sets self._current_process for cancellation.

        Args:
            url: Video URL

        Returns:
            Path to downloaded video file.

        Raises:
            ValueError: If download fails
            TimeoutError: If download takes longer than self.timeout
        """
        output_template = str(self.output_dir / "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "-f", "best[ext=mp4]/best",
            "--output", output_template,
            "--no-warnings",
            "--no-playlist",
            url,
        ]
        logger.info(f"Downloading: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._current_process = proc
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )

            if proc.returncode != 0:
                err_text = stderr.decode(errors="replace")[:500]
                raise ValueError(
                    f"yt-dlp download failed (exit {proc.returncode}): {err_text}"
                )

            # Parse yt-dlp output to find the downloaded file
            combined = stdout.decode(errors="replace") + stderr.decode(errors="replace")

            # Pattern 1: "[Merger] Merging format into "path""
            merge_match = re.search(r'\[Merger\] Merging format[s]? into "([^"]+)"', combined)
            if merge_match:
                p = Path(merge_match.group(1))
                if p.exists():
                    logger.info(f"Downloaded: {p}")
                    return p

            # Pattern 2: "[download] Destination: path"
            dest_match = re.search(r"\[download\] Destination:\s*(.+)", combined)
            if dest_match:
                p = Path(dest_match.group(1).strip())
                if p.exists():
                    logger.info(f"Downloaded: {p}")
                    return p

            # Pattern 3: Plain "Destination: path"
            plain_dest = re.search(r"Destination:\s*(.+)", combined)
            if plain_dest:
                p = Path(plain_dest.group(1).strip())
                if p.exists():
                    logger.info(f"Downloaded: {p}")
                    return p

            # Fallback: try to find by video_id from the output dir
            # yt-dlp typically names files as {id}.{ext}
            info = await self.extract_info(url)
            for f in sorted(self.output_dir.glob(f"{info.video_id}.*"),
                           key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file():
                    logger.info(f"Found downloaded file by ID: {f}")
                    return f

            raise FileNotFoundError(
                f"Download completed but output file not found in {self.output_dir}"
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"yt-dlp download timed out after {self.timeout}s"
            )
        finally:
            self._current_process = None

    # ── Audio Extraction ─────────────────────────────────

    def extract_audio(self, video_path: Path) -> Path:
        """Extract audio from video file using ffmpeg.

        Converts to 16kHz mono 16-bit WAV for Whisper.

        Args:
            video_path: Path to the downloaded video file

        Returns:
            Path to audio file (.wav)

        Raises:
            FileNotFoundError: If video_path doesn't exist
            RuntimeError: If ffmpeg fails
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        audio_path = video_path.with_suffix(".wav")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(audio_path),
        ]
        logger.debug(f"Running: {' '.join(cmd)}")

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg audio extraction failed: {result.stderr[:500]}"
            )
        if not audio_path.exists():
            raise FileNotFoundError(
                f"Audio file not created at {audio_path}"
            )

        logger.info(f"Extracted audio: {audio_path}")
        return audio_path

    # ── Cancellation ─────────────────────────────────────

    def cancel(self):
        """Cancel any running yt-dlp subprocess."""
        if self._current_process and self._current_process.returncode is None:
            logger.warning("Cancelling yt-dlp subprocess...")
            try:
                self._current_process.kill()
            except Exception as e:
                logger.debug(f"Error killing yt-dlp process: {e}")
            self._current_process = None

    # ── Cleanup ──────────────────────────────────────────

    def cleanup(self, video_path: Optional[Path] = None,
                audio_path: Optional[Path] = None):
        """Remove downloaded video and audio files.

        Args:
            video_path: Video file path to remove
            audio_path: Audio file path to remove
        """
        for p in [video_path, audio_path]:
            if p and p.exists():
                p.unlink(missing_ok=True)
                logger.info(f"Cleaned up: {p}")
