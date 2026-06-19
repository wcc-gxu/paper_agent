"""VideoBrowser — CloakBrowser 封装，用于解析视频分享链接。

职责:
  1. 打开分享链接（短链/口令）→ 跟踪所有重定向 → 获取最终视频页面 URL
  2. 从浏览器会话中提取 cookie（Netscape 格式），供 yt-dlp 使用
  3. Cookie 缓存（避免每次启动浏览器）

CloakBrowser 是 Playwright 的直接替换，C++ 源码层指纹补丁，无需 JS 注入。
首次使用自动下载 ~200MB 定制 Chromium。

用法:
    browser = VideoBrowser(cookie_dir=Path("/tmp/cookies"))
    resolved = await browser.resolve("https://v.douyin.com/XXXX/")
    # resolved.final_url = "https://www.douyin.com/video/123456"
    # resolved.cookies_path = "/tmp/cookies/douyin.txt"
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Cookie 缓存有效期（秒）—— 抖音 cookie 通常 30 分钟内有效
COOKIE_TTL_SECONDS = 1800  # 30 minutes

# 共享链接中常见的重定向等待超时
REDIRECT_TIMEOUT_SECONDS = 15

# 页面加载等待超时
PAGE_LOAD_TIMEOUT_SECONDS = 30


@dataclass
class ResolvedLink:
    """浏览器解析后的链接信息。"""
    original_url: str = ""                      # 原始分享链接
    final_url: str = ""                         # 重定向后的最终视频页面 URL
    cookies_path: str = ""                      # Netscape 格式 cookie 文件路径
    page_title: str = ""                        # 页面标题
    platform: str = ""                          # 检测到的平台
    used_cache: bool = False                    # 是否使用了缓存的 cookie


class VideoBrowser:
    """CloakBrowser 封装 — 解析分享链接 + 提取 cookie。

    特点:
      - 自动跟踪所有重定向（包括 JS 重定向和深链 fallback）
      - 持久化 profile → cookie 跨会话保留
      - Cookie 缓存 → 30 分钟内复用，减少浏览器启动次数
      - 优雅降级 → CloakBrowser 不可用时抛清晰错误
    """

    def __init__(
        self,
        cookie_dir: Path,
        headless: bool = True,
        timeout: int = 30,
    ):
        """初始化 VideoBrowser.

        Args:
            cookie_dir: cookie 文件缓存目录
            headless: 是否无头模式（CloakBrowser 已做 headless 指纹补丁）
            timeout: 浏览器操作总超时（秒）
        """
        self._cookie_dir = Path(cookie_dir)
        self._cookie_dir.mkdir(parents=True, exist_ok=True)
        self._headless = headless
        self._timeout = timeout
        self._browser = None
        self._context = None
        self._profile_dir = self._cookie_dir / "browser_profile"

    # ── Public API ───────────────────────────────────────

    async def resolve(self, url: str) -> ResolvedLink:
        """解析分享链接 → 获取最终视频页面 URL + cookie。

        流程:
          1. 检查 cookie 缓存（如未过期则直接复用）
          2. 启动 CloakBrowser → 访问链接 → 等待重定向完成
          3. 提取最终 URL + 页面标题
          4. 导出 cookie → Netscape 格式文件
          5. 关闭浏览器

        Args:
            url: 分享链接（短链/长链/口令文本）

        Returns:
            ResolvedLink(final_url, cookies_path, page_title, platform, used_cache)
        """
        # ── 1. 先检查缓存 ──
        from .video_downloader import detect_platform
        platform = detect_platform(url)
        cached = self._check_cookie_cache(platform)
        if cached:
            logger.info(f"Using cached cookies for {platform}: {cached}")
            return ResolvedLink(
                original_url=url,
                final_url=url,  # 用原始 URL 重试，yt-dlp 带 cookie 后可能成功
                cookies_path=cached,
                platform=platform,
                used_cache=True,
            )

        # ── 2. 启动浏览器解析 ──
        logger.info(f"Launching browser to resolve: {url[:80]}")

        try:
            result = await asyncio.to_thread(self._resolve_sync, url)
        except Exception as e:
            logger.error(f"Browser resolution failed: {e}")
            raise RuntimeError(
                f"浏览器解析链接失败: {e}\n"
                f"请确认已安装 cloakbrowser: pip install cloakbrowser"
            ) from e

        return result

    # ── 同步解析逻辑 (在 asyncio.to_thread 中运行) ──────

    def _resolve_sync(self, url: str) -> ResolvedLink:
        """同步执行浏览器解析。在单独线程中运行。"""
        from .video_downloader import detect_platform

        platform = detect_platform(url)

        try:
            from cloakbrowser import launch
        except ImportError:
            raise RuntimeError(
                "CloakBrowser 未安装。请执行: pip install cloakbrowser"
            )

        # 启动浏览器（首次下载 ~200MB 定制 Chromium）
        browser = launch(
            headless=self._headless,
            timeout=self._timeout * 1000,  # cloakbrowser uses ms
        )

        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.0 Mobile/15E148 Safari/604.1"
                ),
                viewport={"width": 390, "height": 844},  # iPhone 14 size
                locale="zh-CN",
            )
            page = context.new_page()

            # 导航到分享链接
            # 抖音短链会 302 → snssdk1128:// 深链 → ERR_ABORTED
            # 但在此之前浏览器已通过 JS 跳转到网页版
            final_url = url  # fallback
            page_title = ""

            try:
                page.goto(url, wait_until="domcontentloaded",
                         timeout=REDIRECT_TIMEOUT_SECONDS * 1000)
            except Exception as e:
                err_msg = str(e)
                if "ERR_ABORTED" in err_msg or "net::" in err_msg:
                    logger.debug(f"Navigation interrupted (expected for deep links): {err_msg[:100]}")
                else:
                    raise

            # 等待可能的额外重定向完成
            time.sleep(3)

            # 获取当前 URL（重定向后的最终地址）
            try:
                final_url = page.url
                page_title = page.title()
            except Exception:
                pass  # page may have closed after redirect

            logger.info(f"Resolved: {url[:60]} → {final_url[:80]}")

            # 检测平台（用最终 URL 再检测一次）
            resolved_platform = detect_platform(final_url)
            if resolved_platform != "unknown":
                platform = resolved_platform

            # 导出 cookie 为 Netscape 格式
            cookies_path = self._export_cookies_sync(context, platform)

            return ResolvedLink(
                original_url=url,
                final_url=final_url,
                cookies_path=cookies_path,
                page_title=page_title,
                platform=platform,
                used_cache=False,
            )

        finally:
            browser.close()

    # ── Cookie 管理 ──────────────────────────────────────

    def _cookie_file(self, platform: str) -> Path:
        """获取某平台的 cookie 文件路径。"""
        safe_platform = platform.replace("/", "_").replace("\\", "_")
        return self._cookie_dir / f"{safe_platform}_cookies.txt"

    def _check_cookie_cache(self, platform: str) -> Optional[str]:
        """检查是否有未过期的缓存 cookie。

        Returns:
            cookie 文件路径（如有效），否则 None
        """
        cookie_file = self._cookie_file(platform)
        if not cookie_file.exists():
            return None

        # 检查文件年龄
        file_age = time.time() - cookie_file.stat().st_mtime
        if file_age > COOKIE_TTL_SECONDS:
            logger.debug(f"Cookie cache expired for {platform} ({file_age:.0f}s > {COOKIE_TTL_SECONDS}s)")
            return None

        # 检查文件非空
        if cookie_file.stat().st_size == 0:
            return None

        return str(cookie_file)

    def _export_cookies_sync(self, context, platform: str) -> str:
        """从浏览器 context 导出 cookies 为 Netscape 格式。

        Args:
            context: Playwright/CloakBrowser browser context
            platform: 平台名（用于文件命名）

        Returns:
            cookie 文件路径
        """
        cookies = context.cookies()
        cookie_file = self._cookie_file(platform)

        # Netscape cookie 格式（yt-dlp 兼容）
        # 格式: domain\tflag\tpath\tsecure\texpires\tname\tvalue
        lines = ["# Netscape HTTP Cookie File", "# Generated by VideoBrowser", ""]
        for c in cookies:
            domain = c.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expires = str(int(c.get("expires", -1))) if c.get("expires", -1) > 0 else "0"
            name = c.get("name", "")
            value = c.get("value", "")

            lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")

        cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"Exported {len(cookies)} cookies → {cookie_file}")

        # 确保文件权限正确
        cookie_file.chmod(0o600)

        return str(cookie_file)

    # ── Cleanup ──────────────────────────────────────────

    def clear_cookies(self, platform: str = None):
        """清除缓存的 cookie。

        Args:
            platform: 平台名。如为 None，清除所有。
        """
        if platform:
            f = self._cookie_file(platform)
            if f.exists():
                f.unlink()
                logger.info(f"Cleared cookies for {platform}")
        else:
            for f in self._cookie_dir.glob("*_cookies.txt"):
                f.unlink()
            logger.info("Cleared all cached cookies")

    def close(self):
        """释放资源（同步清理）。"""
        self.clear_cookies()
