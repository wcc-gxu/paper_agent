"""HTTP 直接下载器 — 用于支持标准 HTTP/HTTPS PDF 下载的来源."""

import logging
from pathlib import Path
from typing import Optional

import httpx

from ..config import Config

logger = logging.getLogger(__name__)


class HttpDownloader:
    """异步 HTTP PDF 下载器。

    使用 httpx.AsyncClient 直接下载 PDF 文件。
    适用于 arXiv、Semantic Scholar（OA PDF）等直接返回 PDF 链接的来源。
    """

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self, config: Optional[Config] = None, client: Optional[httpx.AsyncClient] = None):
        self.config = config or Config()
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 httpx 异步客户端。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(self.config.download_timeout),
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Accept": "application/pdf,*/*",
                },
            )
        return self._client

    async def download(self, url: str, target_path: Path) -> bool:
        """从 URL 下载 PDF 到 target_path。

        Args:
            url: PDF 文件的远程 URL。
            target_path: 本地保存路径（含文件名）。

        Returns:
            下载成功返回 True，失败返回 False。
        """
        client = await self._get_client()
        try:
            logger.info(f"下载: {url} → {target_path}")
            response = await client.get(url)
            response.raise_for_status()

            content = response.content
            # 验证是否为 PDF（magic bytes: %PDF）
            if len(content) < 4 or content[:4] != b"%PDF":
                logger.warning(
                    f"响应不是有效 PDF（magic bytes: {content[:20]!r}）: {url}"
                )
                return False

            # 确保目标目录存在
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(content)
            logger.info(f"下载完成: {target_path} ({len(content) / 1024:.0f} KB)")
            return True

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP {e.response.status_code} 下载失败: {url}")
            return False
        except httpx.TimeoutException:
            logger.error(f"下载超时 ({self.config.download_timeout}s): {url}")
            return False
        except Exception as e:
            logger.error(f"下载异常: {url} - {e}")
            return False

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
