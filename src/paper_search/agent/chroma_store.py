"""ChromaDB 向量存储 — 论文语义索引."""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CHROMA_PATH = Path("~/.paper_search/chroma").expanduser()


class ChromaStore:
    """ChromaDB 封装 — 论文向量存储与检索。"""

    def __init__(self, persist_dir: Optional[Path] = None):
        self.persist_dir = persist_dir or DEFAULT_CHROMA_PATH
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = None
        self._collection = None

    @property
    def client(self):
        if self._client is None:
            import chromadb
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        return self._client

    @property
    def collection(self):
        if self._collection is None:
            try:
                self._collection = self.client.get_collection("papers")
            except Exception:
                self._collection = self.client.create_collection(
                    name="papers",
                    metadata={"description": "学术论文向量索引"},
                )
        return self._collection

    def add_paper(self, paper_id: str, title: str, abstract: str, metadata: dict = None):
        """将论文添加到向量索引（仅标题+摘要）。"""
        text = f"{title}\n{abstract or ''}"
        meta = metadata or {}
        meta["paper_id"] = paper_id
        try:
            self.collection.add(ids=[paper_id], documents=[text], metadatas=[meta])
        except Exception as e:
            logger.warning(f"ChromaDB add 失败: {e}")

    def add_papers_batch(self, papers: list):
        """批量添加论文到向量索引（仅标题+摘要）。"""
        if not papers:
            return
        ids, docs, metas = [], [], []
        for p in papers:
            pid = getattr(p, 'id', None) or p.get('id', '')
            title = getattr(p, 'title', '') or p.get('title', '')
            abstract = getattr(p, 'abstract', '') or p.get('abstract', '') or ''
            ids.append(pid)
            docs.append(f"{title}\n{abstract}")
            metas.append({"paper_id": pid, "title": title[:200], "year": getattr(p, 'year', None) or p.get('year')})
        try:
            self.collection.add(ids=ids, documents=docs, metadatas=metas)
        except Exception as e:
            logger.warning(f"ChromaDB batch add 失败: {e}")

    # ── 全文分块入库 (Phase 3A 新增) ────────────────────

    def add_chunks(self, chunks: list) -> int:
        """将论文的章节块批量添加到向量索引。

        Args:
            chunks: Chunk 对象列表（来自 SectionChunker）。

        Returns:
            成功添加的块数量。
        """
        if not chunks:
            return 0

        ids, docs, metas = [], [], []
        for c in chunks:
            ids.append(c.id)
            docs.append(f"# {c.section_title}\n\n{c.text}")
            metas.append({
                "paper_id": c.paper_id,
                "section": c.section_title,
                "level": c.section_level,
                "chunk_index": c.chunk_index,
                **c.metadata,
            })

        try:
            self.collection.add(ids=ids, documents=docs, metadatas=metas)
            return len(chunks)
        except Exception as e:
            logger.warning(f"ChromaDB chunk add 失败: {e}")
            return 0

    def search_chunks(self, query: str, paper_id: str = None, n_results: int = 10) -> list[dict]:
        """在全文块中搜索。

        Args:
            query: 搜索查询。
            paper_id: 可选，限定搜索某篇论文的块。
            n_results: 返回结果数。
        """
        where_filter = {"paper_id": paper_id} if paper_id else None
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where_filter,
            )
            chunks = []
            if results and results.get("ids") and results["ids"][0]:
                for i, cid in enumerate(results["ids"][0]):
                    meta = results.get("metadatas", [[{}]])[0][i] if results.get("metadatas") else {}
                    doc = results.get("documents", [[""]])[0][i] if results.get("documents") else ""
                    chunks.append({
                        "chunk_id": cid,
                        "paper_id": meta.get("paper_id", ""),
                        "section": meta.get("section", ""),
                        "text": doc[:500],
                    })
            return chunks
        except Exception as e:
            logger.warning(f"ChromaDB chunk search 失败: {e}")
            return []

    def search_similar(self, query: str, n_results: int = 20) -> list[dict]:
        """语义检索与查询最相似的论文。"""
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results,
            )
            papers = []
            if results and results.get("ids") and results["ids"][0]:
                for i, pid in enumerate(results["ids"][0]):
                    meta = results.get("metadatas", [[{}]])[0][i] if results.get("metadatas") else {}
                    dist = results.get("distances", [[0]])[0][i] if results.get("distances") else 0
                    papers.append({
                        "paper_id": pid,
                        "title": meta.get("title", ""),
                        "distance": dist,
                    })
            return papers
        except Exception as e:
            logger.warning(f"ChromaDB search 失败: {e}")
            return []

    def count(self) -> int:
        try:
            return self.collection.count()
        except Exception:
            return 0
