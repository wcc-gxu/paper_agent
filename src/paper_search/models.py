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


# ═══════════════════════════════════════════════════════════════
# Video Models — 视频解析子 Agent
# ═══════════════════════════════════════════════════════════════


class VideoMetadata(BaseModel):
    """yt-dlp 提取的视频元数据。"""
    url: str = Field(default="", description="规范化后的视频 URL")
    platform: str = Field(default="", description="视频平台 (douyin/tiktok/youtube/...)")
    video_id: str = Field(default="", description="平台视频 ID")
    title: str = Field(default="", description="视频标题")
    duration_seconds: float = Field(default=0.0, description="视频时长 (秒)")
    uploader: str = Field(default="", description="上传者/频道名")
    thumbnail_url: str = Field(default="", description="缩略图 URL")
    description: str = Field(default="", description="视频简介")


class VideoSummary(BaseModel):
    """LLM 结构化视频摘要。"""
    one_line_summary: str = Field(default="", description="一句话总结视频核心内容")
    key_points: list[dict] = Field(
        default_factory=list,
        description="分段要点列表 [{title: str, content: str}]",
    )
    core_thesis: str = Field(default="", description="视频的核心论点或主张")
    tags: list[str] = Field(default_factory=list, description="中英双语标签")
    language: str = Field(default="zh", description="视频主要语言 (zh/en)")


class VideoAnalysis(BaseModel):
    """LLM 视频深度分析。"""
    stance: str = Field(default="", description="视频立场 (中立/赞成/反对/批判/宣传)")
    stance_confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="立场置信度")
    logic_chain: list[dict] = Field(
        default_factory=list,
        description="逻辑链路 [{premise: str, conclusion: str}]",
    )
    factual_claims: list[dict] = Field(
        default_factory=list,
        description="可验证陈述 [{claim, verdict, evidence, confidence}]",
    )
    overall_assessment: str = Field(default="", description="总体评价")
    target_audience: str = Field(default="", description="目标受众分析")
    production_quality: str = Field(default="medium", description="制作质量 (high/medium/low)")


class VideoResult(BaseModel):
    """完整的视频处理结果。"""
    metadata: VideoMetadata = Field(default_factory=VideoMetadata, description="视频元数据")
    local_video_path: str = Field(default="", description="本地视频文件路径")
    transcript_path: str = Field(default="", description="转录文本文件路径")
    transcript_text: Optional[str] = Field(default=None, description="完整转录文本")
    transcription_skipped: bool = Field(default=False, description="是否因长视频跳过转录")
    summary: Optional[VideoSummary] = Field(default=None, description="结构化摘要")
    analysis: Optional[VideoAnalysis] = Field(default=None, description="深度分析")
