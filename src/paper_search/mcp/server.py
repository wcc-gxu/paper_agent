"""MCP Server — 通过 FastMCP 暴露论文搜索与下载工具.

启动方式:
    fastmcp run src/paper_search/mcp/server.py
    或
    python -m paper_search.mcp.server

在 Claude Code 中配置 mcp.json:
    {
      "mcpServers": {
        "paper-search": {
          "command": "python",
          "args": ["-m", "paper_search.mcp.server"]
        }
      }
    }
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Optional

from fastmcp import FastMCP
from pydantic import Field

from ..config import Config
from ..engine import PaperSearchEngine
from ..models import SearchQuery, SourceType

logger = logging.getLogger(__name__)

# ── 创建 MCP 实例 ──────────────────────────────────────

mcp = FastMCP(
    "Paper Search",
    instructions=(
        "学术论文搜索与 PDF 下载引擎。支持 arXiv、PubMed、Semantic Scholar、"
        "CNKI（知网）、IEEE Xplore、ScienceDirect 等多源文献搜索和 PDF 下载。"
    ),
)

# ── 全局引擎实例（惰性初始化） ─────────────────────────

_engine: Optional[PaperSearchEngine] = None


def _get_engine() -> PaperSearchEngine:
    """获取或创建搜索引擎实例。"""
    global _engine
    if _engine is None:
        # 导入所有 provider 以触发注册
        _load_providers()
        _engine = PaperSearchEngine(Config())
    return _engine


def _load_providers():
    """加载所有 Provider 模块。"""
    try:
        from ..providers import arxiv_provider  # noqa: F401
        from ..providers import semanticscholar_provider  # noqa: F401
        from ..providers import pubmed_provider  # noqa: F401
    except ImportError:
        pass
    try:
        from ..providers import cnki_provider  # noqa: F401
    except ImportError:
        pass
    try:
        from ..providers import ieee_provider  # noqa: F401
    except ImportError:
        pass
    try:
        from ..providers import sciencedirect_provider  # noqa: F401
    except ImportError:
        pass


def _paper_to_dict(paper) -> dict:
    """将 Paper 转为简洁的 dict（适合 MCP 上下文窗口）。"""
    return {
        "title": paper.title,
        "authors": paper.authors[:10],
        "year": paper.year,
        "abstract": (paper.abstract[:500] + "..." if paper.abstract and len(paper.abstract) > 500 else paper.abstract),
        "doi": paper.doi,
        "arxiv_id": paper.arxiv_id,
        "pmid": paper.pmid,
        "source": paper.source.value,
        "source_url": paper.source_url,
        "pdf_url": paper.pdf_url,
        "citation_count": paper.citation_count,
        "venue": paper.venue,
    }


# ── MCP Tools ──────────────────────────────────────────


@mcp.tool()
async def search_papers(
    keywords: Annotated[
        str,
        Field(
            description=(
                "搜索关键词，支持 AND/OR 逻辑组合。"
                "例如: 'adversarial attack AND robustness' "
                "或 'large language model evaluation'"
            ),
        ),
    ] = "",
    sources: Annotated[
        str,
        Field(
            description=(
                "搜索来源，逗号分隔。可选值: arxiv, semantic_scholar, pubmed, cnki, ieee, sciencedirect"
                "默认: arxiv,semantic_scholar"
            ),
        ),
    ] = "arxiv,semantic_scholar",
    title: Annotated[
        Optional[str],
        Field(description="按标题精确搜索（设置后 keywords 可留空）"),
    ] = None,
    author: Annotated[
        Optional[str],
        Field(description="按作者筛选，如 'Geoffrey Hinton'"),
    ] = None,
    doi: Annotated[
        Optional[str],
        Field(description="按 DOI 直接查找论文"),
    ] = None,
    year_from: Annotated[
        Optional[int],
        Field(description="起始发表年份，如 2023"),
    ] = None,
    year_to: Annotated[
        Optional[int],
        Field(description="截止发表年份"),
    ] = None,
    max_results: Annotated[
        int,
        Field(description="每个来源最大返回结果数 (1-100, 默认 20)", ge=1, le=100),
    ] = 20,
) -> str:
    """跨多源搜索学术论文。

    返回论文元数据列表（标题、作者、年份、摘要、DOI、PDF链接等）。
    来源包括 arXiv(预印本)、Semantic Scholar(综合)、PubMed(生物医学)、
    以及在校园网环境下可用的 CNKI/IEEE/ScienceDirect。
    """
    engine = _get_engine()

    # 解析来源
    source_list = [SourceType(s.strip().lower()) for s in sources.split(",") if s.strip()]

    query = SearchQuery(
        keywords=keywords,
        title=title,
        author=author,
        doi=doi,
        year_from=year_from,
        year_to=year_to,
        max_results=max_results,
        sources=source_list,
    )

    result = await engine.search(query)

    # 构建简洁的 JSON 响应
    output = {
        "search_term": query.effective_query(),
        "total_found": result.total_found,
        "sources_searched": [s.value for s in source_list],
        "errors": result.errors,
        "papers": [_paper_to_dict(p) for p in result.papers],
    }

    return json.dumps(output, ensure_ascii=False, indent=2)


@mcp.tool()
async def download_paper(
    title: Annotated[
        str,
        Field(description="论文标题（完整或部分）"),
    ],
    source: Annotated[
        str,
        Field(description="来源: arxiv, semantic_scholar, pubmed, cnki, ieee, sciencedirect"),
    ],
    doi: Annotated[
        Optional[str],
        Field(description="论文 DOI（如果有，可精确定位论文）"),
    ] = None,
    output_dir: Annotated[
        str,
        Field(description="下载目录，默认 ~/papers"),
    ] = "~/papers",
) -> str:
    """下载单篇论文的 PDF 文件。

    先搜索定位论文，然后下载 PDF 到本地指定目录。
    文件按 {来源}/{年份}/{作者}_{年份}_{标题}.pdf 格式组织。
    """
    engine = _get_engine()

    source_type = SourceType(source.strip().lower())
    target_dir = Path(output_dir).expanduser().resolve()

    # 构建搜索查询以定位论文
    query = SearchQuery(
        title=title,
        doi=doi,
        keywords=title,
        sources=[source_type],
        max_results=5,
    )
    result = await engine.search(query)

    if not result.papers:
        return json.dumps({
            "success": False,
            "error": f"未找到匹配论文: {title}",
        }, ensure_ascii=False)

    paper = result.papers[0]
    dl_result = await engine.download(paper, target_dir=target_dir)

    return json.dumps({
        "success": dl_result.success,
        "paper_title": paper.title,
        "source": paper.source.value,
        "local_path": dl_result.local_path,
        "error": dl_result.error,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def batch_search(
    file_path: Annotated[
        str,
        Field(description="查询文件的绝对路径（.json 或 .csv）"),
    ],
    download: Annotated[
        bool,
        Field(description="是否同时下载所有论文的 PDF"),
    ] = False,
    default_sources: Annotated[
        str,
        Field(description="默认来源列表，逗号分隔。文件未指定来源时使用"),
    ] = "arxiv,semantic_scholar",
) -> str:
    """从文件读取多个搜索查询并批量执行。

    JSON 格式示例:
    [
        {"keywords": "transformer attention", "sources": ["arxiv"], "max_results": 10},
        {"title": "BERT: Pre-training of Deep Bidirectional Transformers", "sources": ["arxiv", "semantic_scholar"]}
    ]

    CSV 格式示例:
    keywords,sources,max_results
    "large language model",arxiv|semantic_scholar,20
    "image classification",arxiv,10
    """
    engine = _get_engine()

    src_list = [SourceType(s.strip().lower()) for s in default_sources.split(",") if s.strip()]

    try:
        summary = await engine.batch_search_from_file(
            file_path,
            download=download,
            default_sources=src_list,
        )
    except FileNotFoundError:
        return json.dumps({"success": False, "error": f"文件不存在: {file_path}"}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # 构建摘要响应
    papers_preview = []
    for r in summary.results[:3]:  # 只预览前 3 个查询的结果
        papers_preview.append({
            "query": r.query.effective_query(),
            "papers_found": r.total_found,
            "titles": [p.title[:80] for p in r.papers[:5]],
        })

    return json.dumps({
        "success": True,
        "total_queries": summary.total_queries,
        "total_papers_found": summary.total_papers_found,
        "total_downloaded": summary.total_downloaded,
        "total_failed": summary.total_failed,
        "preview": papers_preview,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_sources() -> str:
    """列出所有可用的文献搜索来源及其状态。

    返回每个来源的名称、类型（免费API / 校内IP）、是否可用。
    """
    engine = _get_engine()

    # 确保所有 provider 已加载
    _load_providers()

    from ..providers import list_providers as get_all

    health = await engine.health_check()

    source_descriptions = {
        "arxiv": "arXiv 预印本 — CS/AI/数学/物理等领域",
        "semantic_scholar": "Semantic Scholar — 跨学科综合学术搜索",
        "pubmed": "PubMed/PMC — 生物医学文献数据库",
        "cnki": "中国知网 CNKI — 中文学术论文/学位论文/专利",
        "ieee": "IEEE Xplore — 电子/计算机工程论文",
        "sciencedirect": "Elsevier ScienceDirect — 综合学术期刊",
    }

    sources_info = []
    for st in get_all():
        sources_info.append({
            "name": st.value,
            "description": source_descriptions.get(st.value, ""),
            "available": health.get(st.value, False),
            "type": "校内IP" if st.value in ("cnki", "ieee", "sciencedirect") else "免费API",
        })

    return json.dumps({
        "total_sources": len(sources_info),
        "sources": sources_info,
    }, ensure_ascii=False, indent=2)


# ── Agent 实例 (惰性初始化) ────────────────────────────

_agent = None

def _get_agent():
    global _agent
    if _agent is None:
        from ..agent.agent import ResearchAgent
        _agent = ResearchAgent()
    return _agent


# ── Agent MCP Tools ──────────────────────────────────────

@mcp.tool()
async def research(
    query: Annotated[
        str,
        Field(description="自然语言搜索需求，如 '帮我搜近半年 adversarial attack 的最新进展'"),
    ],
    enable_citation_chase: Annotated[
        bool,
        Field(description="是否在搜索完成后执行引用追踪（1层）"),
    ] = False,
    max_iterations: Annotated[
        int,
        Field(description="最大搜索迭代轮数 (1-5，默认 3)", ge=1, le=5),
    ] = 3,
) -> str:
    """智能学术搜索 — 用自然语言一句话完成搜索全流程。

    自动完成：
    1. 解析搜索意图 → 2. 拆解子查询 → 3. 多源并发搜索
    → 4. AI 逐篇评估相关性 → 5. 自动判断是否继续搜索
    → 6. 下载相关论文 PDF → 7. 生成结构化报告

    报告包含：搜索概况、关键论文、研究方向分类、后续建议。
    """
    agent = _get_agent()

    result = await agent.research(
        user_query=query,
        enable_citation_chase=enable_citation_chase,
        max_iterations=max_iterations,
    )

    # 返回精简版（报告全文在文件中）
    papers_preview = result.get("papers", [])[:10]
    stats = result.get("stats", {})

    return json.dumps({
        "project_id": result["project_id"],
        "stats": stats,
        "report": result["report"],
        "top_papers": papers_preview,
        "report_path": result.get("report_path", ""),
        "errors": stats.get("errors", [])[:5],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def research_status(
    project_id: Annotated[
        str,
        Field(description="搜索项目 ID (从 research tool 返回)"),
    ],
) -> str:
    """查看某个搜索项目的状态和结果摘要。"""
    agent = _get_agent()
    status = await agent.get_status(project_id)
    return json.dumps(status, ensure_ascii=False, indent=2)


@mcp.tool()
async def research_history(
    limit: Annotated[
        int,
        Field(description="返回最近 N 条历史记录 (默认 10)"),
    ] = 10,
) -> str:
    """列出历史搜索项目。"""
    agent = _get_agent()
    projects = await agent.list_history(limit)
    return json.dumps(projects, ensure_ascii=False, indent=2)


@mcp.tool()
async def citation_chase(
    paper_title: Annotated[
        str,
        Field(description="种子论文标题"),
    ],
    doi: Annotated[
        Optional[str],
        Field(description="论文 DOI (如果有，可精确定位)"),
    ] = None,
) -> str:
    """独立引用追踪 — 找到引用了某篇论文的后继研究和它引用的前人工作（1层）。

    可用于单独对某篇关键论文做引用分析。
    """
    from ..models import Paper, SearchQuery, SourceType

    engine = _get_engine()

    # 先找到这篇论文
    query = SearchQuery(title=paper_title, doi=doi, sources=[SourceType.SEMANTIC_SCHOLAR], max_results=1)
    result = await engine.search(query)

    if not result.papers:
        return json.dumps({"error": f"未找到论文: {paper_title}"}, ensure_ascii=False)

    paper = result.papers[0]

    # 通过 Semantic Scholar 查引用和被引
    from ..providers.semanticscholar_provider import SemanticScholarProvider
    s2 = SemanticScholarProvider()

    citations_info = {"seed_paper": paper.title, "doi": paper.doi, "citing": [], "references": []}

    # 查被引用（cited by）
    if paper.doi:
        try:
            async with s2._make_client() as client:
                resp = await client.get(
                    f"/paper/DOI:{paper.doi}/citations",
                    params={"fields": "title,year,authors,url", "limit": 20},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("data", []):
                        cp = item.get("citingPaper", {})
                        citations_info["citing"].append({
                            "title": cp.get("title", ""),
                            "year": cp.get("year"),
                            "url": cp.get("url"),
                        })

                # 查参考文献（references）
                resp2 = await client.get(
                    f"/paper/DOI:{paper.doi}/references",
                    params={"fields": "title,year,authors,url", "limit": 20},
                )
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    for item in data2.get("data", []):
                        rp = item.get("citedPaper", {})
                        citations_info["references"].append({
                            "title": rp.get("title", ""),
                            "year": rp.get("year"),
                            "url": rp.get("url"),
                        })
        except Exception as e:
            citations_info["error"] = str(e)

    return json.dumps(citations_info, ensure_ascii=False, indent=2)


@mcp.tool()
async def export_report(
    project_id: Annotated[
        str,
        Field(description="搜索项目 ID"),
    ],
    format: Annotated[
        str,
        Field(description="导出格式: bibtex 或 json"),
    ] = "bibtex",
) -> str:
    """导出某次搜索的报告（BibTeX 或 JSON 格式）。"""
    agent = _get_agent()
    project = agent.db.get_project(project_id)
    if not project:
        return json.dumps({"error": f"项目不存在: {project_id}"}, ensure_ascii=False)

    papers = agent.db.get_relevant_papers(project_id)

    if format == "bibtex":
        entries = []
        for p in papers:
            authors = json.loads(p.get("authors", "[]"))
            first_author = authors[0].split()[-1] if authors else "Unknown"
            key = f"{first_author}{p.get('year', '????')}{p['title'][:20]}"
            entry = (
                f"@article{{{key},\n"
                f"  title = {{{{{p['title']}}}}},\n"
                f"  author = {{{{' and '.join(authors[:10])}}}},\n"
                f"  year = {{{p.get('year', '????')}}},\n"
                f"  doi = {{{p.get('doi', '')}}},\n"
                f"  journal = {{{p.get('venue', '')}}}\n"
                f"}}"
            )
            entries.append(entry)
        return "\n\n".join(entries)

    else:
        return json.dumps(papers, ensure_ascii=False, indent=2)


# ── 直接运行入口 ──────────────────────────────────────

def main():
    """通过 stdio 运行 MCP Server。"""
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
