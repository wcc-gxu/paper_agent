"""Playwright 浏览器下载器 — 用于需要 JS 渲染和会话认证的 PDF 下载.

适用场景: IEEE Xplore、ScienceDirect 等需要浏览器环境的机构数据库。
支持 Cookie 缓存以减少浏览器启动次数。
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class CookieCache:
    """Cookie 持久化缓存 — 减少 Playwright 浏览器启动次数。

    Cookie 文件存储在 ~/.paper_search/cookies/{source}.json。
    包含过期检测和自动刷新。
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, source: str) -> Path:
        return self.cache_dir / f"{source}.json"

    def load(self, source: str) -> Optional[list[dict]]:
        """加载缓存的 cookies。"""
        path = self._cache_path(source)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cookies = data.get("cookies", [])
            saved_at = data.get("saved_at", 0)

            # Cookie 有效期: 2 小时
            if time.time() - saved_at > 7200:
                logger.info(f"Cookie 缓存已过期 ({source})")
                return None

            if not cookies:
                return None

            logger.info(f"加载 Cookie 缓存: {source} ({len(cookies)} 条)")
            return cookies
        except Exception as e:
            logger.warning(f"读取 Cookie 缓存失败: {e}")
            return None

    def save(self, source: str, cookies: list[dict]) -> None:
        """保存 cookies 到缓存。"""
        path = self._cache_path(source)
        data = {
            "cookies": cookies,
            "saved_at": time.time(),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Cookie 已缓存: {source} ({len(cookies)} 条)")

    def clear(self, source: str) -> None:
        """清除指定来源的 cookie 缓存。"""
        path = self._cache_path(source)
        if path.exists():
            path.unlink()
            logger.info(f"Cookie 缓存已清除: {source}")


class PlaywrightDownloader:
    """基于 Playwright 的浏览器 PDF 下载器。

    用于需要在浏览器中导航、点击按钮、处理重定向的 PDF 下载。
    支持 Cookie 缓存以减少重复启动浏览器的开销。

    用法:
        async with PlaywrightDownloader() as dl:
            cookies = cookie_cache.load("ieee")
            path = await dl.download(
                url="https://ieeexplore.ieee.org/document/xxx",
                target_path=Path("./paper.pdf"),
                auth_cookies=cookies,
                pdf_selector='a.pdf-link, a[href*="pdf"]',
            )
            if not path:
                # 可能 cookie 过期, 重新获取
                ...
    """

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        return self

    async def __aexit__(self, *args):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def download(
        self,
        url: str,
        target_path: Path,
        auth_cookies: Optional[list[dict]] = None,
        pdf_selector: str = 'a[href*="pdf"], a.pdf-link, a[title*="PDF"]',
        click_timeout: int = 15000,
    ) -> Optional[Path]:
        """通过浏览器下载 PDF。

        Args:
            url: 论文页面 URL。
            target_path: 保存路径。
            auth_cookies: 预加载的认证 cookies。
            pdf_selector: PDF 下载按钮的 CSS 选择器。
            click_timeout: 等待 PDF 链接的超时时间 (ms)。

        Returns:
            保存的 PDF 文件路径，失败返回 None。
        """
        target_path.parent.mkdir(parents=True, exist_ok=True)

        context = await self._browser.new_context(
            user_agent=self.USER_AGENT,
            accept_downloads=True,
        )

        if auth_cookies:
            await context.add_cookies(auth_cookies)

        page = await context.new_page()

        try:
            # 导航到论文页面
            logger.info(f"Playwright: 导航到 {url[:120]}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 等待页面稳定
            await asyncio.sleep(2)

            # 方案 1: 点击 PDF 下载按钮触发 download 事件
            download_success = await self._try_click_download(
                page, pdf_selector, target_path, click_timeout
            )
            if download_success:
                # 提取 cookies 供后续使用
                cookies = await context.cookies()
                await context.close()
                return target_path, cookies

            # 方案 2: 从页面中提取 PDF URL，用 httpx 下载
            pdf_url = await self._extract_pdf_url(page)
            if pdf_url:
                logger.info(f"Playwright: 提取到 PDF URL: {pdf_url[:120]}")
                # 用页面 cookies 构造 httpx 请求
                cookies = await context.cookies()
                await context.close()

                # 使用 httpx 下载（带 cookies）
                success = await self._http_download_with_cookies(
                    pdf_url, target_path, cookies
                )
                if success:
                    return target_path, cookies

            await context.close()
            return None

        except Exception as e:
            logger.error(f"Playwright 下载异常: {e}")
            await context.close()
            return None

    async def _try_click_download(
        self, page, selector: str, target_path: Path, timeout: int
    ) -> bool:
        """尝试点击 PDF 按钮触发浏览器下载。"""
        try:
            # 等待 PDF 链接出现
            await page.wait_for_selector(selector, timeout=timeout)

            # 设置下载监听
            download_future = asyncio.get_event_loop().create_future()

            async def on_download(download):
                await download.save_as(str(target_path))
                download_future.set_result(True)

            page.on("download", on_download)

            # 点击 PDF 链接
            pdf_el = await page.query_selector(selector)
            if pdf_el:
                await pdf_el.click()
                await asyncio.wait_for(download_future, timeout=30)
                logger.info(f"Playwright: 下载完成 → {target_path}")
                return True

        except asyncio.TimeoutError:
            logger.debug("Playwright: PDF 按钮未出现或下载超时")
        except Exception as e:
            logger.debug(f"Playwright: 点击下载失败: {e}")

        return False

    async def _extract_pdf_url(self, page) -> Optional[str]:
        """从页面中提取 PDF 下载链接。"""
        try:
            # 尝试各种常见的 PDF 链接模式
            selectors = [
                'a[href*=".pdf"]',
                'a[href*="/pdf/"]',
                'a[href*="download"]',
                'a[href*="stampPDF"]',
                'meta[name="citation_pdf_url"]',
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    href = await el.get_attribute("href")
                    if not href:
                        content = await el.get_attribute("content")
                        if content:
                            href = content
                    if href and "pdf" in href.lower():
                        # 处理相对 URL
                        if href.startswith("/"):
                            base_url = page.url
                            from urllib.parse import urljoin
                            href = urljoin(base_url, href)
                        return href

            # 从页面文本中搜索 PDF URL
            page_text = await page.content()
            import re
            pdf_patterns = [
                r'https?://[^"\'\s]+\.pdf',
                r'https?://[^"\'\s]+/stampPDF/[^"\'\s]+',
                r'https?://[^"\'\s]+/pdf/[^"\'\s]+',
            ]
            for pattern in pdf_patterns:
                match = re.search(pattern, page_text, re.I)
                if match:
                    return match.group(0)

        except Exception as e:
            logger.debug(f"Playwright: 提取 PDF URL 失败: {e}")

        return None

    async def _http_download_with_cookies(
        self, url: str, target_path: Path, cookies: list[dict]
    ) -> bool:
        """使用 cookies 通过 httpx 下载 PDF。"""
        import httpx

        # 转换 Playwright cookies 到 httpx 格式
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in cookies
        )

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=60.0,
            headers={
                "User-Agent": self.USER_AGENT,
                "Cookie": cookie_header,
                "Accept": "application/pdf,*/*",
            },
        ) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()

                content = resp.content
                if len(content) >= 4 and content[:4] == b"%PDF":
                    target_path.write_bytes(content)
                    logger.info(f"PDF 下载成功: {target_path} ({len(content)/1024:.0f} KB)")
                    return True
                else:
                    logger.warning(f"下载的不是 PDF: {content[:20]!r}")
                    return False
            except Exception as e:
                logger.error(f"HTTP 下载失败: {e}")
                return False

    async def capture_cookies(self, url: str) -> list[dict]:
        """访问页面并捕获 cookies（用于首次认证）。"""
        context = await self._browser.new_context(
            user_agent=self.USER_AGENT,
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)  # 等待可能的重定向和 cookie 设置
            cookies = await context.cookies()
            logger.info(f"捕获 cookies: {len(cookies)} 条")
            return cookies
        finally:
            await context.close()
