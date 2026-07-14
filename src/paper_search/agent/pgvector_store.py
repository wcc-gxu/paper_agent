"""pgvector 向量存储 — 替代 ChromaDB，使用 PostgreSQL + pgvector。

API 完全兼容 ChromaStoreV2，调用方只需更换导入路径即可切换到 pgvector 后端。

使用示例:
    store = PgVectorStore(user_id="user-default")
    store.add_paper_abstract("pap-001", "My Title", "Abstract text...")
    results = store.search_similar("query text", n_results=10)

环境变量:
    DATABASE_URL: PostgreSQL 连接字符串
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _get_embedding(texts: list[str], model: str | None = None) -> list[list[float]]:
    """调用 Embedding API 生成向量。

    使用独立的 EMBEDDING_* 环境变量配置，与 LLM chat API 完全分离。
    当前配置为火山引擎 Agent Plan (doubao-embedding-vision, 1024 维)。

    如果 API 不可用，返回零向量作为占位（调用方应处理这种情况）。
    """
    from paper_search.config import EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS

    if not texts:
        return []

    api_key = EMBEDDING_API_KEY
    if not api_key:
        logger.warning("EMBEDDING_API_KEY 未设置，embedding 返回零向量")
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    model_name = model or EMBEDDING_MODEL
    emb_url = EMBEDDING_BASE_URL.rstrip("/") + "/embeddings"

    try:
        import requests
        resp = requests.post(
            emb_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_name,
                "input": texts,
                "dimensions": EMBEDDING_DIMENSIONS,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            embeddings = [item["embedding"] for item in data["data"]]
            actual_dim = len(embeddings[0]) if embeddings else EMBEDDING_DIMENSIONS
            if actual_dim != EMBEDDING_DIMENSIONS:
                logger.warning(
                    f"Embedding 维度不匹配: 期望 {EMBEDDING_DIMENSIONS}, 实际 {actual_dim}"
                )
            return embeddings
        else:
            logger.warning(f"Embedding API 返回 {resp.status_code}: {resp.text[:200]}")
            return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]
    except Exception as e:
        logger.warning(f"Embedding API 调用失败: {e}")
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]


class PgVectorStore:
    """PostgreSQL + pgvector 向量存储，API 兼容 ChromaStoreV2。

    与 ChromaStoreV2 不同，本类需要 user_id 参数用于多用户数据隔离。
    所有查询自动附加 WHERE user_id = $user_id 过滤条件。
    """

    COLLECTION_ABSTRACT = "papers_abstract"   # 映射到 paper_chunks (chunk_type='abstract')
    COLLECTION_FULLTEXT = "papers_fulltext"   # 映射到 paper_chunks (chunk_type='body')
    COLLECTION_TERMS = "glossary_terms"       # 映射到 glossary_embeddings

    def __init__(self, user_id: str = "anonymous", dsn: str = None):
        """初始化 pgvector 存储。

        Args:
            user_id: 用户 ID，用于数据隔离。
            dsn: PostgreSQL 连接字符串，默认从 DATABASE_URL 环境变量读取。
        """
        import psycopg2
        import psycopg2.extras

        self.user_id = user_id
        self.dsn = dsn or os.environ.get("DATABASE_URL", "")
        self._conn = None
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras

    @property
    def conn(self):
        """惰性获取数据库连接。"""
        if self._conn is None or self._conn.closed:
            if not self.dsn:
                raise RuntimeError(
                    "DATABASE_URL 未设置，无法连接 PostgreSQL。"
                    "请设置环境变量 DATABASE_URL 或传入 dsn 参数。"
                )
            self._conn = self._psycopg2.connect(self.dsn)
            self._conn.autocommit = False
        return self._conn

    def close(self):
        """关闭数据库连接。"""
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    # ── Embedding 生成 ───────────────────────────────────

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """为文本列表生成 embedding 向量。"""
        if not texts:
            return []
        return _get_embedding(texts)

    def _format_vector(self, embedding: list[float]) -> str:
        """将 embedding 列表格式化为 pgvector 兼容字符串。"""
        if not embedding:
            return None
        return f"[{','.join(str(x) for x in embedding)}]"

    # ── 摘要索引 ────────────────────────────────────────

    def add_paper_abstract(
        self, paper_id: str, title: str, abstract: str, metadata: dict = None
    ):
        """将论文标题+摘要索引到 paper_chunks 表（chunk_type='abstract'）。

        Args:
            paper_id: 论文 ID。
            title: 论文标题。
            abstract: 摘要文本。
            metadata: 可选的额外元数据字典。
        """
        text = f"{title}\n{abstract or ''}"
        meta = metadata or {}
        meta["paper_id"] = paper_id
        meta["title"] = title[:200]

        try:
            emb = self._embed_texts([text])[0]
            cur = self.conn.cursor()
            chunk_id = f"chk-{paper_id}-abstract"
            cur.execute(
                """INSERT INTO paper_chunks (id, paper_id, user_id, chunk_text, chunk_type,
                   section_title, section_level, chunk_order, embedding, token_count, metadata)
                   VALUES (%s, %s, %s, %s, 'abstract', 'abstract', 0, 0, %s::vector, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                   chunk_text=EXCLUDED.chunk_text, embedding=EXCLUDED.embedding, metadata=EXCLUDED.metadata""",
                (
                    chunk_id,
                    paper_id,
                    self.user_id,
                    text,
                    self._format_vector(emb),
                    len(text.split()),
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
            self.conn.commit()
        except Exception as e:
            logger.warning(f"PgVectorStore add_paper_abstract 失败 ({paper_id}): {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass

    def add_paper_chunk(self, paper_id: str, text: str, section: str = "",
                         index: int = 0, metadata: dict = None) -> bool:
        """添加单个论文文本块（兼容旧版 ChromaStore API）。

        Args:
            paper_id: 论文 ID。
            text: 块文本内容。
            section: 块所在的章节标题。
            index: 块在论文中的序号。
            metadata: 可选的额外元数据。

        Returns:
            True 如果成功。
        """
        if not text or not text.strip():
            return False
        try:
            emb = self._embed_texts([text])[0]
            meta = metadata or {}
            meta["paper_id"] = paper_id
            meta["section"] = section[:200]
            chunk_id = f"chk-{paper_id}-{index:04d}"
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO paper_chunks (id, paper_id, user_id, chunk_text, chunk_type,
                   section_title, section_level, chunk_order, embedding, token_count, metadata)
                   VALUES (%s, %s, %s, %s, 'body', %s, 0, %s, %s::vector, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET
                   chunk_text=EXCLUDED.chunk_text, embedding=EXCLUDED.embedding, metadata=EXCLUDED.metadata""",
                (
                    chunk_id, paper_id, self.user_id, text, section[:200], index,
                    self._format_vector(emb), len(text.split()),
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.warning(f"PgVectorStore add_paper_chunk 失败 ({paper_id}): {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def add_abstracts_batch(self, items: list[dict]):
        """批量添加摘要索引。

        Args:
            items: [{"paper_id": "...", "title": "...", "abstract": "...", ...}, ...]
        """
        if not items:
            return

        try:
            texts = [f"{it.get('title', '')}\n{it.get('abstract', '') or ''}" for it in items]
            embeddings = self._embed_texts(texts)

            cur = self.conn.cursor()
            for item, emb in zip(items, embeddings):
                pid = item["paper_id"]
                title = item.get("title", "")
                abstract = item.get("abstract", "") or ""
                text = f"{title}\n{abstract}"
                meta = {
                    "paper_id": pid,
                    "title": title[:200],
                    "year": item.get("year"),
                    "source": item.get("source"),
                    "venue": item.get("venue"),
                }
                chunk_id = f"chk-{pid}-abstract"

                cur.execute(
                    """INSERT INTO paper_chunks (id, paper_id, user_id, chunk_text, chunk_type,
                       section_title, section_level, chunk_order, embedding, token_count, metadata)
                       VALUES (%s, %s, %s, %s, 'abstract', 'abstract', 0, 0, %s::vector, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET
                       chunk_text=EXCLUDED.chunk_text, embedding=EXCLUDED.embedding, metadata=EXCLUDED.metadata""",
                    (
                        chunk_id,
                        pid,
                        self.user_id,
                        text,
                        self._format_vector(emb),
                        len(text.split()),
                        json.dumps(meta, ensure_ascii=False),
                    ),
                )
            self.conn.commit()
        except Exception as e:
            logger.warning(f"PgVectorStore add_abstracts_batch 失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass

    def search_abstract(self, query: str, n_results: int = 20) -> list[dict]:
        """通过摘要语义搜索论文。"""
        try:
            emb = self._embed_texts([query])[0]
            cur = self.conn.cursor(cursor_factory=self._extras.RealDictCursor)
            cur.execute(
                """SELECT pc.paper_id, pc.metadata->>'title' AS title,
                   1 - (pc.embedding <=> %s::vector) AS similarity
                   FROM paper_chunks pc
                   WHERE pc.user_id = %s AND pc.chunk_type = 'abstract'
                   ORDER BY pc.embedding <=> %s::vector
                   LIMIT %s""",
                (self._format_vector(emb), self.user_id, self._format_vector(emb), n_results),
            )
            rows = cur.fetchall()
            return [
                {
                    "paper_id": r["paper_id"],
                    "title": r.get("title", ""),
                    "distance": 1.0 - r["similarity"],  # 保持与 ChromaDB 兼容（distance 越小越相似）
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"PgVectorStore search_abstract 失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return []

    # ── 向后兼容 ChromaStore (旧版) ─────────────────────

    def search_similar(self, query: str, n_results: int = 20) -> list[dict]:
        """语义检索与查询最相似的论文（兼容旧版 ChromaStore API）。

        搜索所有 chunk_type 的 paper_chunks，按 paper_id 去重后返回。
        返回字段: paper_id, title, score (相似度 0~1), chunk_text, abstract
        """
        try:
            emb = self._embed_texts([query])[0]
            cur = self.conn.cursor(cursor_factory=self._extras.RealDictCursor)
            # 使用 DISTINCT ON 按 paper_id 去重，取每个 paper 最相似的 chunk
            cur.execute(
                """SELECT DISTINCT ON (pc.paper_id) pc.paper_id,
                   pc.metadata->>'title' AS title,
                   pc.chunk_text,
                   pc.metadata->>'abstract' AS abstract,
                   1 - (pc.embedding <=> %s::vector) AS similarity
                   FROM paper_chunks pc
                   WHERE pc.user_id = %s
                   ORDER BY pc.paper_id, pc.embedding <=> %s::vector
                   LIMIT %s""",
                (self._format_vector(emb), self.user_id, self._format_vector(emb), n_results),
            )
            rows = cur.fetchall()
            return [
                {
                    "paper_id": r["paper_id"],
                    "title": r.get("title", ""),
                    "score": float(r["similarity"]),
                    "chunk_text": r.get("chunk_text", ""),
                    "abstract": r.get("abstract", ""),
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"PgVectorStore search_similar 失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return []

    # ── 全文分块索引 ────────────────────────────────────

    def add_fulltext_chunks(self, chunks: list) -> int:
        """将论文章节分块索引到 paper_chunks 表（chunk_type='body'）。

        Args:
            chunks: Chunk 对象列表（通常来自 SectionChunker），
                    每个 chunk 需要有 .id, .paper_id, .section_title,
                    .text, .section_level, .chunk_index, .metadata 属性。

        Returns:
            成功添加的块数量。
        """
        if not chunks:
            return 0

        try:
            texts = [f"# {c.section_title}\n\n{c.text}" for c in chunks]
            embeddings = self._embed_texts(texts)

            cur = self.conn.cursor()
            count = 0
            for c, emb in zip(chunks, embeddings):
                meta = {
                    "paper_id": c.paper_id,
                    "section": c.section_title,
                    "level": c.section_level,
                    "chunk_index": c.chunk_index,
                    **c.metadata,
                }
                cur.execute(
                    """INSERT INTO paper_chunks (id, paper_id, user_id, chunk_text, chunk_type,
                       section_title, section_level, chunk_order, embedding, token_count, metadata)
                       VALUES (%s, %s, %s, %s, 'body', %s, %s, %s, %s::vector, %s, %s)
                       ON CONFLICT (id) DO UPDATE SET
                       chunk_text=EXCLUDED.chunk_text, embedding=EXCLUDED.embedding, metadata=EXCLUDED.metadata""",
                    (
                        c.id,
                        c.paper_id,
                        self.user_id,
                        texts[count],
                        c.section_title,
                        c.section_level,
                        c.chunk_index,
                        self._format_vector(emb),
                        len(c.text.split()) if c.text else 0,
                        json.dumps(meta, ensure_ascii=False),
                    ),
                )
                count += 1

            self.conn.commit()
            return len(chunks)
        except Exception as e:
            logger.warning(f"PgVectorStore add_fulltext_chunks 失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return 0

    def search_fulltext(
        self, query: str, paper_id: str = None, n_results: int = 10
    ) -> list[dict]:
        """在全文块中语义搜索。

        Args:
            query: 搜索查询文本。
            paper_id: 可选，限定搜索某篇论文的块。
            n_results: 返回结果数。
        """
        try:
            emb = self._embed_texts([query])[0]
            cur = self.conn.cursor(cursor_factory=self._extras.RealDictCursor)

            if paper_id:
                cur.execute(
                    """SELECT pc.id AS chunk_id, pc.paper_id,
                       pc.section_title AS section, pc.chunk_text AS text
                       FROM paper_chunks pc
                       WHERE pc.user_id = %s AND pc.paper_id = %s AND pc.chunk_type = 'body'
                       ORDER BY pc.embedding <=> %s::vector
                       LIMIT %s""",
                    (self.user_id, paper_id, self._format_vector(emb), n_results),
                )
            else:
                cur.execute(
                    """SELECT pc.id AS chunk_id, pc.paper_id,
                       pc.section_title AS section, pc.chunk_text AS text
                       FROM paper_chunks pc
                       WHERE pc.user_id = %s AND pc.chunk_type = 'body'
                       ORDER BY pc.embedding <=> %s::vector
                       LIMIT %s""",
                    (self.user_id, self._format_vector(emb), n_results),
                )

            rows = cur.fetchall()
            return [
                {
                    "chunk_id": r["chunk_id"],
                    "paper_id": r["paper_id"],
                    "section": r.get("section", ""),
                    "text": (r.get("text") or "")[:500],
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"PgVectorStore search_fulltext 失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return []

    # ── 术语词表 ────────────────────────────────────────

    def add_terms_batch(self, terms: list[dict]) -> int:
        """批量添加术语到 glossary_embeddings 表。

        Args:
            terms: [{"en_term": "...", "zh_term": "...", "context": "..."}, ...]

        Returns:
            成功添加的术语数量。
        """
        if not terms:
            return 0

        valid_terms = []
        for t in terms:
            en = t.get("en_term", "") or ""
            if en:
                zh = t.get("zh_term", "") or ""
                valid_terms.append((en, zh, t.get("context", "")[:200]))

        if not valid_terms:
            return 0

        try:
            texts = [f"{en}\n{zh}" for en, zh, _ in valid_terms]
            embeddings = self._embed_texts(texts)

            cur = self.conn.cursor()
            count = 0
            for (en, zh, ctx), emb in zip(valid_terms, embeddings):
                gle_id = f"gle-{hashlib.md5(en.encode()).hexdigest()[:16]}"
                cur.execute(
                    """INSERT INTO glossary_embeddings (id, glossary_term_id, term_text, embedding)
                       VALUES (%s, NULL, %s, %s::vector)
                       ON CONFLICT (id) DO UPDATE SET
                       term_text=EXCLUDED.term_text, embedding=EXCLUDED.embedding""",
                    (gle_id, f"{en}\n{zh}", self._format_vector(emb)),
                )
                count += 1

            self.conn.commit()
            return count
        except Exception as e:
            logger.warning(f"PgVectorStore add_terms_batch 失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return 0

    # ── 消息向量召回 (Phase 4) ──────────────────────────

    def add_message_embedding(self, session_id: str, msg_id: str,
                               text: str, user_id: str) -> bool:
        """将 message/reply 文本嵌入并存入 message_embeddings 表。

        Args:
            session_id: 会话 ID
            msg_id: ws_messages 中的消息 ID
            text: message/reply 的 payload.content 原始文本
            user_id: 用户 ID

        Returns:
            True 如果成功，False 如果失败
        """
        if not text or not text.strip():
            return False

        text = text.strip()[:4000]  # 截断过长的消息文本
        try:
            emb = self._embed_texts([text])[0]
            emb_id = f"meb-{msg_id}"
            cur = self.conn.cursor()
            cur.execute(
                """INSERT INTO message_embeddings (id, session_id, msg_id, user_id, content_text, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s::vector)
                   ON CONFLICT (id) DO UPDATE SET
                   content_text=EXCLUDED.content_text, embedding=EXCLUDED.embedding""",
                (emb_id, session_id, msg_id, user_id, text, self._format_vector(emb)),
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.warning(f"PgVectorStore add_message_embedding 失败 ({msg_id[:8]}): {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def search_similar_messages(self, query_text: str, user_id: str,
                                 threshold: float = 0.75, limit: int = 5) -> list[dict]:
        """语义检索与查询文本相似的历史消息。

        使用余弦相似度（1 - cosine_distance）。
        可选 Redis 缓存避免重复的 embedding API 调用。

        Args:
            query_text: 用户当前消息文本
            user_id: 用户 ID（数据隔离）
            threshold: 相似度阈值（默认 0.75），只返回 >= threshold 的结果
            limit: 最大返回数量

        Returns:
            [{"msg_id": "...", "content_text": "...", "similarity": 0.85, "session_id": "..."}, ...]
        """
        if not query_text or not query_text.strip():
            return []

        # ── Redis 缓存（可选） ──
        cache_key = f"vec:cache:{user_id}:{hashlib.md5(query_text.encode()).hexdigest()}"
        cache_ttl = int(os.environ.get("VECTOR_CACHE_TTL", "900"))  # 默认 15 分钟
        redis_url = os.environ.get("REDIS_URL", "")

        if redis_url:
            try:
                import redis
                r = redis.from_url(redis_url, decode_responses=True)
                cached = r.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass  # Redis 不可用时降级到直接查 PG

        # ── 查询 PG ──
        try:
            emb = self._embed_texts([query_text])[0]
            cur = self.conn.cursor(cursor_factory=self._extras.RealDictCursor)
            similarity_expr = f"1 - (embedding <=> %s::vector)"
            cur.execute(
                f"""SELECT msg_id, session_id, content_text,
                       {similarity_expr} AS similarity
                   FROM message_embeddings
                   WHERE user_id = %s
                     AND 1 - (embedding <=> %s::vector) >= %s
                   ORDER BY embedding <=> %s::vector
                   LIMIT %s""",
                (self._format_vector(emb), user_id, self._format_vector(emb),
                 threshold, self._format_vector(emb), limit),
            )
            rows = cur.fetchall()
            results = [
                {
                    "msg_id": r["msg_id"],
                    "session_id": r["session_id"],
                    "content_text": r["content_text"],
                    "similarity": round(float(r["similarity"]), 4),
                }
                for r in rows
            ]

            # ── 写缓存 ──
            if redis_url and results:
                try:
                    import redis
                    r = redis.from_url(redis_url, decode_responses=True)
                    r.setex(cache_key, cache_ttl, json.dumps(results, ensure_ascii=False))
                except Exception:
                    pass

            return results
        except Exception as e:
            logger.warning(f"PgVectorStore search_similar_messages 失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return []

    # ── 工具方法 ────────────────────────────────────────

    def get_embedding(self, paper_id: str) -> Optional[list[float]]:
        """获取某篇论文的 embedding 向量（用于聚类等）。

        Args:
            paper_id: 论文 ID。

        Returns:
            embedding 列表（维度由 EMBEDDING_DIMENSIONS 决定），如果未找到则返回 None。
        """
        try:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT embedding FROM paper_chunks
                   WHERE paper_id = %s AND user_id = %s AND chunk_type = 'abstract'
                   LIMIT 1""",
                (paper_id, self.user_id),
            )
            row = cur.fetchone()
            if row and row[0]:
                # pgvector 返回的格式可能是字符串或列表
                emb = row[0]
                if isinstance(emb, str):
                    emb = [float(x) for x in emb.strip("[]").split(",")]
                return list(emb)
            return None
        except Exception as e:
            logger.warning(f"PgVectorStore get_embedding 失败 ({paper_id}): {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return None

    def count_abstract(self) -> int:
        """获取摘要 chunk 数量。"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM paper_chunks WHERE user_id = %s AND chunk_type = 'abstract'",
                (self.user_id,),
            )
            return cur.fetchone()[0]
        except Exception:
            return 0

    def count_fulltext(self) -> int:
        """获取全文 chunk 数量。"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM paper_chunks WHERE user_id = %s AND chunk_type = 'body'",
                (self.user_id,),
            )
            return cur.fetchone()[0]
        except Exception:
            return 0

    def count(self) -> int:
        """获取全部 chunk 数量（兼容旧版 ChromaStore）。"""
        return self.count_abstract() + self.count_fulltext()

    # ── 用户相关 ────────────────────────────────────────

    def count_user_papers(self) -> int:
        """获取用户已索引的独立论文数。"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT COUNT(DISTINCT paper_id) FROM paper_chunks WHERE user_id = %s",
                (self.user_id,),
            )
            return cur.fetchone()[0]
        except Exception:
            return 0
