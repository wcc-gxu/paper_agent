"""统一数据模型 — 所有模块共享的 Pydantic 模型."""

from datetime import date
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    """论文来源类型。"""
    ARXIV = "arxiv"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    PUBMED = "pubmed"
    CNKI = "cnki"
    IEEE = "ieee"
    SCIENCEDIRECT = "sciencedirect"


class SearchQuery(BaseModel):
    """统一搜索查询 — 跨所有来源。"""
    keywords: str = Field(
        default="",
        description="搜索关键词，支持 AND/OR 逻辑，如 'adversarial attack AND robustness'",
    )
    title: Optional[str] = Field(
        default=None, description="按标题精确搜索，设置后 keywords 将被忽略"
    )
    author: Optional[str] = Field(
        default=None, description="按作者筛选"
    )
    doi: Optional[str] = Field(
        default=None, description="按 DOI 直接查找"
    )
    year_from: Optional[int] = Field(
        default=None, ge=1900, description="起始发表年份"
    )
    year_to: Optional[int] = Field(
        default=None, ge=1900, description="截止发表年份"
    )
    max_results: int = Field(
        default=20, ge=1, le=100, description="每个来源最大返回结果数"
    )
    sources: list[SourceType] = Field(
        default_factory=lambda: [SourceType.ARXIV, SourceType.SEMANTIC_SCHOLAR],
        description="要搜索的来源列表",
    )

    def effective_query(self) -> str:
        """返回实际搜索用的查询字符串。"""
        if self.doi:
            return self.doi
        if self.title:
            return self.title
        if self.author:
            q = self.author
            if self.keywords:
                q = f"{q} {self.keywords}"
            return q
        return self.keywords


class Paper(BaseModel):
    """统一的论文结果 — 各 Provider 将来源数据映射为该结构。"""
    title: str = Field(description="论文标题")
    authors: list[str] = Field(default_factory=list, description="作者列表")
    year: Optional[int] = Field(default=None, description="发表年份")
    abstract: Optional[str] = Field(default=None, description="摘要")
    doi: Optional[str] = Field(default=None, description="DOI")
    arxiv_id: Optional[str] = Field(default=None, description="arXiv ID")
    pmid: Optional[str] = Field(default=None, description="PubMed ID")
    source: SourceType = Field(description="来源数据库")
    source_url: Optional[str] = Field(default=None, description="论文在来源网站的 URL")
    pdf_url: Optional[str] = Field(default=None, description="PDF 直接下载链接（如已知）")
    citation_count: Optional[int] = Field(default=None, description="引用次数")
    venue: Optional[str] = Field(default=None, description="发表的期刊/会议")
    keywords: list[str] = Field(default_factory=list, description="关键词")

    def identifier(self) -> str:
        """返回此论文的最佳标识符（DOI > arXiv ID > PMID > 标题）。"""
        return self.doi or self.arxiv_id or self.pmid or self.title


class SearchResult(BaseModel):
    """多源搜索的聚合结果。"""
    query: SearchQuery = Field(description="原始搜索查询")
    papers: list[Paper] = Field(default_factory=list, description="去重后的论文列表")
    total_found: int = Field(default=0, description="去重后总数")
    errors: list[str] = Field(default_factory=list, description="各来源的错误信息")


class DownloadResult(BaseModel):
    """单篇论文的下载结果。"""
    paper: Paper = Field(description="被下载的论文")
    local_path: str = Field(default="", description="保存到本地的绝对路径")
    success: bool = Field(default=False, description="下载是否成功")
    error: Optional[str] = Field(default=None, description="失败原因（如有）")


class BatchSummary(BaseModel):
    """批量操作的汇总结果。"""
    total_queries: int = 0
    total_papers_found: int = 0
    total_downloaded: int = 0
    total_failed: int = 0
    results: list[SearchResult] = Field(default_factory=list)
    downloads: list[DownloadResult] = Field(default_factory=list)
