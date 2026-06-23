"""ChromaDB 向量存储 — 论文语义索引."""

import logging
from pathlib import Path
from typing import Optional

from ..config import get_chroma_path

logger = logging.getLogger(__name__)

DEFAULT_CHROMA_PATH = get_chroma_path()


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


class ChromaStoreV2:
    """ChromaDB 双 Collection 版本 — 摘要索引 + 全文分块索引。

    - papers_abstract: title + abstract → 快速论文筛选
    - papers_fulltext: 章节分块文本 → 深度语义检索
    两者通过 paper_id 关联到 SQLite 论文元数据。
    """

    COLLECTION_ABSTRACT = "papers_abstract"
    COLLECTION_FULLTEXT = "papers_fulltext"
    COLLECTION_TERMS = "glossary_terms"

    def __init__(self, persist_dir: Optional[Path] = None):
        self.persist_dir = persist_dir or DEFAULT_CHROMA_PATH
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = None
        self._collections: dict[str, object] = {}

    @property
    def client(self):
        if self._client is None:
            import chromadb
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        return self._client

    def _get_collection(self, name: str) -> object:
        """惰性获取或创建指定名称的 Collection。"""
        if name not in self._collections:
            try:
                self._collections[name] = self.client.get_collection(name)
            except Exception:
                descriptions = {
                    self.COLLECTION_ABSTRACT: "论文摘要向量索引 — 标题+摘要用于快速筛选",
                    self.COLLECTION_FULLTEXT: "论文全文分块向量索引 — 章节级深度检索",
                }
                self._collections[name] = self.client.create_collection(
                    name=name,
                    metadata={"description": descriptions.get(name, "")},
                )
        return self._collections[name]

    @property
    def abstract_collection(self):
        return self._get_collection(self.COLLECTION_ABSTRACT)

    @property
    def fulltext_collection(self):
        return self._get_collection(self.COLLECTION_FULLTEXT)

    @property
    def terms_collection(self):
        return self._get_collection(self.COLLECTION_TERMS)

    def get_embedding(self, paper_id: str) -> Optional[list[float]]:
        """获取某篇论文摘要的 embedding 向量(用于聚类)。

        优先用 ChromaDB 的 include=["embeddings"];若该 collection 未存
        embedding(老数据)则回退用 paper_id 自身 query 取最近邻的 embedding。
        """
        try:
            res = self.abstract_collection.get(
                ids=[paper_id], include=["embeddings"],
            )
            embs = res.get("embeddings") if res else None
            if embs and len(embs) > 0 and embs[0] is not None:
                return list(embs[0])
            return None
        except Exception as e:
            logger.warning(f"ChromaDB get_embedding 失败 ({paper_id}): {e}")
            return None

    def add_terms_batch(self, terms: list[dict]) -> int:
        """批量添加术语到 glossary_terms collection。

        terms: [{"en_term": "...", "zh_term": "...", "context": "..."}, ...]
        """
        if not terms:
            return 0
        ids, docs, metas = [], [], []
        for t in terms:
            en = t.get("en_term", "") or ""
            zh = t.get("zh_term", "") or ""
            if not en:
                continue
            ids.append(en)
            docs.append(f"{en}\n{zh}")
            metas.append({"en_term": en[:200], "zh_term": zh[:200],
                          "context": (t.get("context") or "")[:200]})
        if not ids:
            return 0
        try:
            self.terms_collection.add(ids=ids, documents=docs, metadatas=metas)
            return len(ids)
        except Exception as e:
            logger.warning(f"ChromaDB terms batch add 失败: {e}")
            return 0

    # ── 摘要索引 ────────────────────────────────────────

    def add_paper_abstract(self, paper_id: str, title: str, abstract: str,
                           metadata: dict = None):
        """将论文标题+摘要索引到 papers_abstract collection。"""
        text = f"{title}\n{abstract or ''}"
        meta = metadata or {}
        meta["paper_id"] = paper_id
        meta["title"] = title[:200]
        try:
            self.abstract_collection.add(
                ids=[paper_id], documents=[text], metadatas=[meta]
            )
        except Exception as e:
            logger.warning(f"ChromaDB abstract add 失败 ({paper_id}): {e}")

    def add_abstracts_batch(self, items: list[dict]):
        """批量添加摘要索引。

        items: [{"paper_id": "...", "title": "...", "abstract": "...", ...}, ...]
        """
        if not items:
            return
        ids, docs, metas = [], [], []
        for item in items:
            pid = item["paper_id"]
            title = item.get("title", "")
            abstract = item.get("abstract", "") or ""
            ids.append(pid)
            docs.append(f"{title}\n{abstract}")
            metas.append({
                "paper_id": pid,
                "title": title[:200],
                "year": item.get("year"),
                "source": item.get("source"),
                "venue": item.get("venue"),
            })
        try:
            self.abstract_collection.add(ids=ids, documents=docs, metadatas=metas)
        except Exception as e:
            logger.warning(f"ChromaDB abstract batch add 失败: {e}")

    def search_abstract(self, query: str, n_results: int = 20) -> list[dict]:
        """通过摘要语义搜索论文。"""
        try:
            results = self.abstract_collection.query(
                query_texts=[query], n_results=n_results,
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
            logger.warning(f"ChromaDB abstract search 失败: {e}")
            return []

    # ── 全文分块索引 ────────────────────────────────────

    def add_fulltext_chunks(self, chunks: list) -> int:
        """将论文章节分块索引到 papers_fulltext collection。

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
            self.fulltext_collection.add(ids=ids, documents=docs, metadatas=metas)
            return len(chunks)
        except Exception as e:
            logger.warning(f"ChromaDB fulltext chunk add 失败: {e}")
            return 0

    def search_fulltext(self, query: str, paper_id: str = None,
                        n_results: int = 10) -> list[dict]:
        """在全文块中语义搜索。

        Args:
            query: 搜索查询。
            paper_id: 可选，限定搜索某篇论文的块。
            n_results: 返回结果数。
        """
        where_filter = {"paper_id": paper_id} if paper_id else None
        try:
            results = self.fulltext_collection.query(
                query_texts=[query], n_results=n_results, where=where_filter,
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
            logger.warning(f"ChromaDB fulltext search 失败: {e}")
            return []

    def count_abstract(self) -> int:
        try:
            return self.abstract_collection.count()
        except Exception:
            return 0

    def count_fulltext(self) -> int:
        try:
            return self.fulltext_collection.count()
        except Exception:
            return 0
