"""CNKI (知网) Provider — 校内IP搜索和下载中文论文.

认证流程:
1. 先尝试用缓存 cookies 直接搜索 (httpx)
2. 如果被重定向到验证码页面, 提示用户启动 Playwright 浏览器手动验证
3. 用户完成滑块验证后, 捕获 cookies 缓存到本地复用

CNKI 搜索接口:
- 新版: POST https://kns.cnki.net/kns8/defaultresult/index (JS 重度依赖)
- 旧版: GET https://kns.cnki.net/kns/brief/result.aspx (HTML 友好)
- 详情: GET https://kns.cnki.net/kcms2/article/abstract?v={v}
- PDF: 从详情页提取链接或构造 download URL

文件格式: 优先 PDF，CAJ 记录后跳过。
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from ..config import Config
from ..models import Paper, SearchQuery, SourceType
from . import register
from .base import BaseProvider

logger = logging.getLogger(__name__)

REQUEST_DELAY = 3.0  # CNKI 请求间隔（秒）


@register(SourceType.CNKI)
class CnkiProvider(BaseProvider):
    """知网 CNKI 论文搜索与下载 Provider。

    需要校园网IP直连 + 可能需要手动完成滑块验证码。
    """

    def __init__(self, config: Optional[Config] = None):
        super().__init__(config)
        self._client: Optional[httpx.Client] = None
        self._cookie_cache = None

    @property
    def source_type(self) -> SourceType:
        return SourceType.CNKI

    # ── HTTP 客户端 ──────────────────────────────────────

    def _get_client(self) -> httpx.Client:
        """获取 httpx 客户端（加载缓存 cookies）。"""
        if self._client is None:
            self._client = httpx.Client(
                follow_redirects=True,
                timeout=30.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            # 加载缓存 cookies
            self._load_cached_cookies()
        return self._client

    def _get_cookie_cache(self):
        """懒加载 CookieCache。"""
        if self._cookie_cache is None:
            from ..downloaders.browser_downloader import CookieCache
            self._cookie_cache = CookieCache(self.config.cookie_cache_dir)
        return self._cookie_cache

    def _load_cached_cookies(self):
        """加载缓存的 CNKI cookies 到客户端。"""
        cache = self._get_cookie_cache()
        cookies = cache.load("cnki")
        if cookies and self._client:
            for c in cookies:
                self._client.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))

    def _detect_captcha(self, response) -> bool:
        """检测是否触发了验证码。"""
        url_lower = str(response.url).lower()
        if "verify" in url_lower or "captcha" in url_lower:
            return True
        text = response.text[:5000].lower() if response.text else ""
        if "验证码" in text or "captcha" in text or "blockpuzzle" in text or "滑块" in text:
            return True
        return False

    # ── Playwright 手动验证 ───────────────────────────────

    async def _acquire_cookies_via_playwright(self) -> bool:
        """启动 Playwright 浏览器，等待用户手动完成 CNKI 验证码。

        打开非 headless 浏览器窗口，用户完成滑块验证后
        按回车键，程序捕获 cookies 并缓存。

        Returns:
            True 表示成功获取 cookies。
        """
        from playwright.async_api import async_playwright

        print("\n" + "=" * 60)
        print("  CNKI 触发了验证码，需要手动验证")
        print("=" * 60)
        print("  1. 即将打开浏览器窗口")
        print("  2. 请在浏览器中完成滑块验证")
        print("  3. 验证通过后，在此终端按 Enter 继续")
        print("=" * 60 + "\n")

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()

                await page.goto(
                    "https://kns.cnki.net/kns8/defaultresult/index?korder=SU",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

                # 等待用户完成验证
                print("  请在浏览器中完成 CNKI 验证码...")
                print("  完成后按 Enter 继续...")
                await asyncio.get_event_loop().run_in_executor(None, input)

                # 捕获 cookies
                cookies = await context.cookies()
                await browser.close()

                if cookies:
                    cache = self._get_cookie_cache()
                    cache.save("cnki", cookies)
                    # 重新加载到客户端
                    self._load_cached_cookies()
                    logger.info(f"CNKI: 成功获取 {len(cookies)} 条 cookies")
                    print(f"  ✓ 已缓存 {len(cookies)} 条 cookies\n")
                    return True
                else:
                    logger.warning("CNKI: 未获取到 cookies")
                    return False

        except Exception as e:
            logger.error(f"CNKI Playwright 验证异常: {e}")
            return False

    # ── 搜索 ──────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> list[Paper]:
        """搜索知网论文。"""
        search_text = self._build_search_text(query)
        if not search_text:
            return []

        return await asyncio.to_thread(self._search_sync, query, search_text)

    def _search_sync(self, query: SearchQuery, search_text: str) -> list[Paper]:
        """同步搜索逻辑 — 旧版 brief 接口 + kns8 降级。"""
        client = self._get_client()
        time.sleep(REQUEST_DELAY)

        # 尝试 brief 旧版接口
        papers = self._search_brief(client, search_text, query)
        if papers:
            return papers[: query.max_results]

        # 尝试 kns8
        papers = self._search_kns8(client, search_text, query)
        return papers[: query.max_results]

    async def search_with_auth(self, query: SearchQuery) -> list[Paper]:
        """带自动认证的搜索 — 如果触发 CAPTCHA，启动 Playwright 手动验证后重试。

        这是推荐的搜索入口，对用户透明地处理 CNKI 验证码。
        """
        # 第一次尝试
        papers = await self.search(query)
        if papers:
            return papers

        # 检查客户端状态 —— 是否被重定向到验证码
        client = self._get_client()
        try:
            test_url = f"https://kns.cnki.net/kns/brief/result.aspx?dbprefix=CJFQ&key={quote('test')}"
            resp = client.get(test_url, follow_redirects=False)
            if resp.status_code in (301, 302):
                location = resp.headers.get("Location", "")
                if "verify" in location.lower():
                    logger.info("CNKI: 需要手动验证")
                    # 启动 Playwright 手动验证
                    success = await self._acquire_cookies_via_playwright()
                    if success:
                        # 重试搜索
                        return await self.search(query)
        except Exception:
            pass

        return papers

    def _search_brief(self, client: httpx.Client, search_text: str, query: SearchQuery) -> list[Paper]:
        """旧版 brief 接口搜索。"""
        encoded = quote(search_text)
        url = (
            f"https://kns.cnki.net/kns/brief/result.aspx?"
            f"dbprefix=CJFQ&key={encoded}"
        )
        try:
            resp = client.get(
                url,
                headers={"Referer": "https://kns.cnki.net/"},
                follow_redirects=False,
            )

            # 检查验证码重定向
            if resp.status_code in (301, 302):
                location = resp.headers.get("Location", "")
                if "verify" in location.lower() or "captcha" in location.lower():
                    logger.warning("CNKI: 被重定向到验证码页面，请运行 search_with_auth() 进行手动验证")
                    return []

            # 跟随非验证码重定向
            if resp.status_code in (301, 302):
                resp = client.get(url, follow_redirects=True)
                resp.raise_for_status()
            else:
                resp.raise_for_status()

            if self._detect_captcha(resp):
                logger.warning("CNKI: 触发验证码，请运行 search_with_auth() 进行手动验证")
                return []

            return self._parse_brief_results(resp.text)

        except httpx.HTTPStatusError as e:
            logger.warning(f"CNKI brief HTTP {e.response.status_code}")
            return []
        except Exception as e:
            logger.warning(f"CNKI brief 异常: {e}")
            return []

    def _search_kns8(self, client: httpx.Client, search_text: str, query: SearchQuery) -> list[Paper]:
        """kns8 新版接口搜索（降级）。"""
        field = "SU$%=|"
        if query.title:
            field = "TI$%=|"
        elif query.author:
            field = "AU$%=|"

        form_data = {
            "searchType": "MulityTermsSearch",
            "searchWord": f"{field}{search_text}",
            "pageNum": "1",
            "pageSize": str(min(query.max_results, 50)),
            "korder": "SU",
            "uniplatform": "NZKPT",
        }
        try:
            resp = client.post(
                "https://kns.cnki.net/kns8/defaultresult/index",
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": "https://kns.cnki.net/kns8/defaultresult/index",
                },
            )

            if self._detect_captcha(resp):
                return []

            resp.raise_for_status()
            return self._parse_kns8_results(resp.text)

        except Exception as e:
            logger.debug(f"CNKI kns8 失败: {e}")
            return []

    # ── HTML 解析 ─────────────────────────────────────────

    def _parse_brief_results(self, html: str) -> list[Paper]:
        """解析旧版 brief 搜索结果。"""
        soup = BeautifulSoup(html, "lxml")
        papers: list[Paper] = []

        rows = soup.select("table.GridTableContent tr")[1:]
        for row in rows:
            try:
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                link = cells[1].find("a") if len(cells) > 1 else None
                if not link:
                    continue

                title = link.get_text(strip=True)
                href = link.get("href", "")
                if href and not href.startswith("http"):
                    href = f"https://kns.cnki.net{href}"

                authors_text = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                source_text = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                year_text = cells[4].get_text(strip=True) if len(cells) > 4 else ""

                paper = Paper(
                    title=title,
                    authors=self._parse_authors(authors_text),
                    year=self._extract_year(year_text),
                    source=SourceType.CNKI,
                    source_url=href,
                    venue=source_text,
                )
                papers.append(paper)
            except Exception as e:
                logger.debug(f"CNKI 解析行: {e}")

        logger.info(f"CNKI (brief): 解析 {len(papers)} 篇")
        return papers

    def _parse_kns8_results(self, html: str) -> list[Paper]:
        """解析 kns8 搜索结果。"""
        soup = BeautifulSoup(html, "lxml")
        papers: list[Paper] = []

        for row in soup.select("tr.result-item, tr.result-item-tr, div.result-item"):
            try:
                link = row.select_one("a.fz14, a.result-title, td.name a")
                if not link:
                    link = row.find("a")
                if not link:
                    continue

                title = link.get_text(strip=True)
                href = link.get("href", "")
                if href and not href.startswith("http"):
                    href = f"https://kns.cnki.net{href}"

                author_el = row.select_one("td.author, span.author")
                author_text = author_el.get_text(strip=True) if author_el else ""

                source_el = row.select_one("td.source, span.source")
                source_text = source_el.get_text(strip=True) if source_el else ""

                year_text = ""
                year_el = row.select_one("td.year, td.date")
                if year_el:
                    year_text = year_el.get_text(strip=True)

                paper = Paper(
                    title=title,
                    authors=self._parse_authors(author_text),
                    year=self._extract_year(year_text),
                    source=SourceType.CNKI,
                    source_url=href,
                    venue=source_text,
                )
                papers.append(paper)
            except Exception:
                pass

        logger.info(f"CNKI (kns8): 解析 {len(papers)} 篇")
        return papers

    # ── PDF 下载 ──────────────────────────────────────────

    async def resolve_pdf_url(self, paper: Paper) -> Optional[str]:
        """从知网详情页解析 PDF 下载链接。"""
        if not paper.source_url:
            return None
        return await asyncio.to_thread(self._resolve_pdf_sync, paper)

    def _resolve_pdf_sync(self, paper: Paper) -> Optional[str]:
        client = self._get_client()
        time.sleep(REQUEST_DELAY)

        try:
            resp = client.get(
                paper.source_url,
                headers={"Referer": "https://kns.cnki.net/"},
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # 查找 PDF 链接
            for sel in [
                "a.btn-download[href*='pdf']",
                "a.download-link[href*='pdf']",
                "a[href*='.pdf']",
            ]:
                el = soup.select_one(sel)
                if el and el.get("href"):
                    href = el["href"]
                    if href.startswith("/"):
                        href = f"https://kns.cnki.net{href}"
                    return href

            # 从 URL 提取 v 参数构造下载 URL
            v_match = re.search(r"[?&]v=([^&]+)", paper.source_url)
            if v_match:
                return (
                    f"https://kns.cnki.net/kcms2/article/download?"
                    f"v={v_match.group(1)}&uniplatform=NZKPT"
                )
        except Exception as e:
            logger.debug(f"CNKI 解析 PDF URL: {e}")

        return None

    async def download_pdf(self, paper: Paper, target_dir: Path) -> Optional[Path]:
        """下载知网 PDF。"""
        pdf_url = await self.resolve_pdf_url(paper)
        if not pdf_url:
            return None

        from ..downloaders.http_downloader import HttpDownloader
        from ..downloaders.filename_utils import build_pdf_filename, ensure_unique_path

        filename = build_pdf_filename(paper, "author_year_title")
        target_path = ensure_unique_path(target_dir / filename)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        client = self._get_client()
        time.sleep(REQUEST_DELAY)

        try:
            resp = client.get(
                pdf_url,
                headers={
                    "Referer": paper.source_url or "https://kns.cnki.net/",
                    "Accept": "application/pdf,*/*",
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            content = resp.content

            if len(content) < 4 or content[:4] != b"%PDF":
                if b"CAJ" in content[:100]:
                    logger.warning(f"CNKI: CAJ 格式(非PDF), 跳过: {paper.title[:60]}")
                else:
                    logger.warning(f"CNKI: 非PDF文件: {content[:20]!r}")
                return None

            target_path.write_bytes(content)
            logger.info(f"CNKI PDF: {target_path} ({len(content)/1024:.0f} KB)")
            return target_path

        except Exception as e:
            logger.error(f"CNKI PDF 下载失败: {e}")
            return None

    # ── 健康检查 ──────────────────────────────────────────

    async def health_check(self) -> bool:
        """检查知网连通性（校内IP + 无验证码）。"""
        try:
            client = self._get_client()
            resp = client.get(
                "https://kns.cnki.net/kns/brief/result.aspx?dbprefix=CJFQ&key=test",
                follow_redirects=False,
                timeout=15.0,
            )
            if resp.status_code in (301, 302):
                loc = resp.headers.get("Location", "").lower()
                if "verify" in loc or "captcha" in loc:
                    logger.info("CNKI: 触发了验证码页面，需手动验证")
                    return False  # 可连接但需要验证码
            if resp.status_code == 200 and "知网" not in resp.text and "cnki" not in resp.text.lower():
                if self._detect_captcha(resp):
                    return False
        except Exception as e:
            logger.debug(f"CNKI 健康检查: {e}")
            return False

        return True

    # ── 工具方法 ──────────────────────────────────────────

    @staticmethod
    def _parse_authors(text: str) -> list[str]:
        if not text:
            return []
        return [a.strip() for a in re.split(r"[;；,，\s]+", text.strip()) if a.strip() and len(a.strip()) > 1]

    @staticmethod
    def _extract_year(text: str) -> Optional[int]:
        if not text:
            return None
        m = re.search(r"(19|20)\d{2}", text)
        if m:
            return int(m.group(0))
        return None

    def close(self):
        """关闭资源（同步）。"""
        if self._client:
            self._client.close()
            self._client = None
