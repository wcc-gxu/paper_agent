"""PubMed/PMC Provider — 通过 NCBI Entrez API 搜索生物医学论文."""

import asyncio
import logging
from typing import Optional

import httpx
from Bio import Entrez

from ..config import Config
from ..models import Paper, SearchQuery, SourceType
from . import register
from .base import BaseProvider

logger = logging.getLogger(__name__)

# NCBI Entrez 基础 URL
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


@register(SourceType.PUBMED)
class PubMedProvider(BaseProvider):
    """PubMed/PMC 论文搜索与下载 Provider。

    使用 Bio.Entrez (NCBI E-utilities API):
    - 需要提供 email（NCBI 要求）
    - 免费无 Key: 3 req/sec
    - 有 API Key: 10 req/sec
    """

    @property
    def source_type(self) -> SourceType:
        return SourceType.PUBMED

    def _configure_entrez(self):
        """配置 Biopython Entrez 全局设置。"""
        email = self.config.ncbi_email or "user@example.com"
        Entrez.email = email
        Entrez.sleep_between_tries = 1

    async def search(self, query: SearchQuery) -> list[Paper]:
        """通过 PubMed API 搜索论文。"""
        search_text = self._build_search_text(query)
        if not search_text:
            logger.warning("PubMed: 搜索词为空，跳过搜索")
            return []

        self._configure_entrez()

        try:
            # Step 1: esearch 获取 PMID 列表
            search_term = self._build_entrez_term(query)
            handle = await asyncio.to_thread(
                Entrez.esearch,
                db="pubmed",
                term=search_term,
                retmax=min(query.max_results, 100),
                sort="relevance",
                retmode="json",
            )
            import json
            search_data = json.loads(handle.read() if hasattr(handle, 'read') else handle)
            handle.close() if hasattr(handle, 'close') else None

            id_list = search_data.get("esearchresult", {}).get("idlist", [])
            if not id_list:
                logger.info(f"PubMed: 未找到匹配论文 (query={search_text[:60]})")
                return []

            # Step 2: efetch 获取详细信息
            handle = await asyncio.to_thread(
                Entrez.efetch,
                db="pubmed",
                id=",".join(id_list),
                rettype="xml",
                retmode="xml",
            )
            records = Entrez.read(handle)
            handle.close() if hasattr(handle, 'close') else None

            # Step 3: 解析为 Paper 对象
            import asyncio as aio

            async def parse_all():
                return await aio.to_thread(self._parse_records, records, id_list)

            papers = await parse_all()

        except Exception as e:
            logger.error(f"PubMed 搜索失败: {e}")
            return []

        logger.info(f"PubMed: 找到 {len(papers)} 篇论文 (query={search_text[:60]})")
        return papers

    def _build_entrez_term(self, query: SearchQuery) -> str:
        """构建 NCBI Entrez 搜索表达式。"""
        parts = []
        if query.title:
            parts.append(f'{query.title}[Title]')
        elif query.keywords:
            parts.append(query.keywords)
        if query.author:
            parts.append(f'{query.author}[Author]')
        if query.doi:
            parts.append(f'{query.doi}[AID]')
        if query.year_from:
            parts.append(f'{query.year_from}:{query.year_to or "3000"}[PDAT]')
        elif query.year_to:
            parts.append(f'1800:{query.year_to}[PDAT]')
        return " AND ".join(parts) if parts else ""

    def _parse_records(self, records, id_list: list[str]) -> list[Paper]:
        """解析 NCBI Entrez XML 响应。"""
        papers: list[Paper] = []
        articles = records.get("PubmedArticle", [])

        for article in articles:
            try:
                medline = article.get("MedlineCitation", {})
                article_info = medline.get("Article", {})

                # 标题
                title = article_info.get("ArticleTitle", "Untitled")

                # 作者
                author_list = article_info.get("AuthorList", [])
                authors = []
                for a in author_list:
                    last = a.get("LastName", "")
                    fore = a.get("ForeName", "")
                    if last:
                        authors.append(f"{fore} {last}".strip() if fore else last)

                # 年份
                year = None
                date_info = article_info.get("Journal", {}).get("JournalIssue", {}).get("PubDate", {})
                if date_info.get("Year"):
                    try:
                        year = int(date_info["Year"])
                    except (ValueError, TypeError):
                        pass

                # 摘要
                abstract_parts = article_info.get("Abstract", {}).get("AbstractText", [])
                if isinstance(abstract_parts, list):
                    abstract = " ".join(str(p) for p in abstract_parts)
                else:
                    abstract = str(abstract_parts) if abstract_parts else None

                # DOI
                doi = None
                eid_list = article_info.get("ELocationID", [])
                if isinstance(eid_list, list):
                    for eid in eid_list:
                        eid_str = str(eid)
                        if eid_str.startswith("doi:"):
                            doi = eid_str[4:].strip()

                # PMID
                pmid = str(medline.get("PMID", ""))

                # 期刊
                journal = article_info.get("Journal", {})
                venue = journal.get("Title")

                # URL
                source_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None

                papers.append(Paper(
                    title=title,
                    authors=authors,
                    year=year,
                    abstract=abstract,
                    doi=doi,
                    pmid=pmid,
                    source=SourceType.PUBMED,
                    source_url=source_url,
                    pdf_url=None,  # 需通过 resolve_pdf_url 获取
                    venue=venue,
                ))
            except Exception as e:
                logger.warning(f"PubMed: 解析记录失败: {e}")

        return papers

    async def resolve_pdf_url(self, paper: Paper) -> Optional[str]:
        """尝试为 PubMed 论文找到可下载的 PDF URL。

        策略:
        1. 通过 PMC ID 查找 OA 全文
        2. 通过 metapub 查找 OA 链接
        3. 通过 Unpaywall 查找 OA 版本
        """
        # 策略 1: 检查 PMC 开放获取
        if paper.pmid:
            try:
                self._configure_entrez()
                handle = await asyncio.to_thread(
                    Entrez.elink,
                    dbfrom="pubmed",
                    db="pmc",
                    id=paper.pmid,
                    retmode="json",
                )
                import json
                data = json.loads(handle.read() if hasattr(handle, 'read') else handle)
                handle.close() if hasattr(handle, 'close') else None

                linksets = data.get("linksets", [])
                for ls in linksets:
                    for link in ls.get("linksetdbs", []):
                        if link.get("linkname") == "pubmed_pmc":
                            pmc_ids = link.get("links", [])
                            if pmc_ids:
                                pmc_id = pmc_ids[0]
                                return f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/"
            except Exception:
                pass

        # 策略 2: 尝试 metapub
        if paper.pmid:
            try:
                import metapub
                finder = await asyncio.to_thread(metapub.FindIt, paper.pmid)
                url = finder.url if finder.url else None
                if url and url.endswith(".pdf"):
                    return url
            except Exception:
                pass

        # 策略 3: 用 DOI 查询 Unpaywall
        if paper.doi:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(
                        f"https://api.unpaywall.org/v2/{paper.doi}",
                        params={"email": self.config.ncbi_email or "user@example.com"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        best = data.get("best_oa_location") or {}
                        pdf_url = best.get("url_for_pdf")
                        if pdf_url:
                            return pdf_url
            except Exception:
                pass

        return None

    async def health_check(self) -> bool:
        """检查 PubMed API 连通性。"""
        try:
            self._configure_entrez()
            handle = await asyncio.to_thread(
                Entrez.esearch, db="pubmed", term="test", retmax=1, retmode="json"
            )
            import json
            data = json.loads(handle.read() if hasattr(handle, 'read') else handle)
            handle.close() if hasattr(handle, 'close') else None
            return "esearchresult" in data
        except Exception:
            return False
