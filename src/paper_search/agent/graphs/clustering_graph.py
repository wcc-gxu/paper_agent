"""ClusteringAgent — 研究方向自动聚类与聚焦。

5 节点线性 Execute Graph:
  load → cluster → label → visualize → detect

功能:
  - 加载项目论文 Embedding
  - K-means 聚类 + HDBSCAN 密度检测
  - LLM 为每个聚类生成主题标签
  - t-SNE/UMAP 降维用于可视化
  - 新方向检测 (不属于任何聚类的 outlier)

输出:
  ~/papers/outputs/{project_id}/clusters.json
  ~/papers/outputs/{project_id}/landscape.json
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)


def _add(left: list, right: list) -> list:
    return (left or []) + (right or [])


# ═══════════════════════════════════════════════════════════════
# State
# ═══════════════════════════════════════════════════════════════


class ClusterState(TypedDict, total=False):
    project_id: str
    papers: list[dict]               # 论文列表 (含 embedding)
    n_clusters: int                  # 聚类数 (auto: 0 表示自动确定)

    # 聚类结果
    embeddings: list[list[float]]    # 所有论文的向量
    cluster_labels: list[int]        # 每篇论文的聚类标签
    cluster_names: dict[int, str]    # {label: "方向名称"}
    cluster_papers: dict[int, list]  # {label: [paper_ids]}

    # 异常检测
    outliers: list[dict]             # 不属于任何聚类的论文
    new_directions: list[dict]       # LLM 评估为潜在新方向的论文组

    # 可视化
    coords_2d: list[list[float]]     # t-SNE/UMAP 2D 坐标
    landscape_path: str

    # 输出
    result: Optional[dict]
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════
# ClusteringAgent
# ═══════════════════════════════════════════════════════════════


class ClusteringAgent:
    """研究方向聚类 Agent — 5 节点线性图。

    图结构:
      load → cluster → label → visualize → detect
    """

    def __init__(self, db, chroma_store, llm, on_progress=None):
        self._db = db
        self._chroma = chroma_store
        self._llm = llm
        self._on_progress = on_progress
        self._graph = None

    def compile(self, checkpointer=None):
        builder = StateGraph(ClusterState)

        builder.add_node("load", self._load_node)
        builder.add_node("cluster", self._cluster_node)
        builder.add_node("label", self._label_node)
        builder.add_node("visualize", self._visualize_node)
        builder.add_node("detect", self._detect_node)

        builder.add_edge(START, "load")
        builder.add_edge("load", "cluster")
        builder.add_edge("cluster", "label")
        builder.add_edge("label", "visualize")
        builder.add_edge("visualize", "detect")
        builder.add_edge("detect", END)

        self._graph = builder.compile(checkpointer=checkpointer)
        return self._graph

    @property
    def graph(self):
        if self._graph is None:
            raise RuntimeError("ClusteringAgent not compiled")
        return self._graph

    # ── 节点 ─────────────────────────────────────────

    async def _load_node(self, state: ClusterState) -> dict:
        """加载论文 Embedding。"""
        project_id = state["project_id"]
        await self._notify("加载论文", 1, 5, "加载论文向量")

        papers = self._db.get_project_papers(project_id)
        if not papers:
            papers = self._db.get_project_papers(project_id, relevant_only=False)

        embeddings = []
        valid_papers = []
        for p in papers:
            pid = p.get("id", "")
            # 从 ChromaDB 获取 embedding
            if self._chroma:
                emb = self._chroma.get_embedding(pid)
                if emb:
                    embeddings.append(emb)
                    valid_papers.append(dict(p))

        logger.info(f"Loaded {len(valid_papers)} papers with embeddings (from {len(papers)} total)")
        return {"papers": valid_papers, "embeddings": embeddings,
                "n_clusters": state.get("n_clusters", 0)}

    async def _cluster_node(self, state: ClusterState) -> dict:
        """K-means / HDBSCAN 聚类。"""
        papers = state.get("papers", [])
        embeddings = state.get("embeddings", [])
        n_papers = len(papers)
        await self._notify("聚类分析", 2, 5, f"聚类 {n_papers} 篇论文")

        if n_papers < 3:
            return {"cluster_labels": [0] * n_papers, "n_clusters": 1}

        try:
            import numpy as np
            X = np.array(embeddings)

            # 自动确定聚类数
            n_clusters = state.get("n_clusters", 0)
            if n_clusters <= 0:
                n_clusters = max(2, min(8, int(np.sqrt(n_papers))))
                if n_papers < 10:
                    n_clusters = min(n_clusters, n_papers // 2)

            from sklearn.cluster import KMeans
            kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
            labels = kmeans.fit_predict(X).tolist()

            logger.info(f"Clustered {n_papers} papers into {n_clusters} groups")
            return {"cluster_labels": labels, "n_clusters": n_clusters}

        except ImportError:
            logger.warning("sklearn not available — using mock clustering")
            n_clusters = max(1, min(4, n_papers // 3))
            labels = [i % n_clusters for i in range(n_papers)]
            return {"cluster_labels": labels, "n_clusters": n_clusters}
        except Exception as e:
            logger.error(f"Clustering failed: {e}")
            return {"error": str(e)}

    async def _label_node(self, state: ClusterState) -> dict:
        """LLM 为每个聚类生成主题标签。"""
        papers = state.get("papers", [])
        labels = state.get("cluster_labels", [])
        n_clusters = state.get("n_clusters", 0)
        await self._notify("主题标注", 3, 5, f"为 {n_clusters} 个聚类生成标签")

        # 按聚类分组
        cluster_papers: dict[int, list] = {}
        for i, p in enumerate(papers):
            lbl = labels[i] if i < len(labels) else 0
            cluster_papers.setdefault(lbl, []).append(p)

        # LLM 为每个聚类生成名称
        cluster_names = {}
        for lbl, cps in sorted(cluster_papers.items()):
            titles = [p.get("title", "")[:80] for p in cps[:5]]
            title_str = "\n".join(f"  - {t}" for t in titles)

            try:
                result = await self._llm.chat_json(
                    messages=[{"role": "user", "content": (
                        f"以下 {len(cps)} 篇论文属于同一研究方向，请为该方向命名（2-5个词）：\n{title_str}"
                    )}],
                    system="你是一个研究方向命名器。输出纯 JSON: {\"name\": \"方向名\", \"keywords\": [\"关键词\"]}",
                    node="cluster_label",
                )
                cluster_names[lbl] = result.get("name", f"方向 {lbl + 1}")
            except Exception as e:
                logger.warning(f"Cluster labeling LLM failed for cluster {lbl}: {e}, using fallback name")
                cluster_names[lbl] = f"研究方向 {lbl + 1}"

        logger.info(f"Cluster labels: {cluster_names}")
        return {"cluster_names": cluster_names, "cluster_papers": cluster_papers}

    async def _visualize_node(self, state: ClusterState) -> dict:
        """t-SNE/UMAP 降维 → 可视化坐标。"""
        embeddings = state.get("embeddings", [])
        papers = state.get("papers", [])
        await self._notify("可视化", 4, 5, "生成研究全景图")

        coords_2d = []
        try:
            if len(embeddings) >= 5:
                import numpy as np
                X = np.array(embeddings)

                try:
                    from sklearn.manifold import TSNE
                    tsne = TSNE(n_components=2, random_state=42, perplexity=min(30, len(X) - 1))
                    coords_2d = tsne.fit_transform(X).tolist()
                except ImportError:
                    # 简单 PCA 降级
                    U, S, Vt = np.linalg.svd(X - X.mean(axis=0), full_matrices=False)
                    coords_2d = (X @ Vt[:2].T).tolist()

            # 保存
            from ...config import get_outputs_dir
            out_dir = get_outputs_dir() / (state.get("project_id", "default"))
            out_dir.mkdir(parents=True, exist_ok=True)

            landscape = {
                "papers": [{"id": p.get("id", ""), "title": p.get("title", ""),
                            "cluster": state.get("cluster_labels", [])[i] if i < len(state.get("cluster_labels", [])) else -1,
                            "x": coords_2d[i][0] if i < len(coords_2d) else 0,
                            "y": coords_2d[i][1] if i < len(coords_2d) else 0}
                           for i, p in enumerate(papers)],
                "clusters": {str(k): v for k, v in state.get("cluster_names", {}).items()},
            }
            landscape_path = out_dir / "landscape.json"
            landscape_path.write_text(json.dumps(landscape, ensure_ascii=False, indent=2))

            return {"coords_2d": coords_2d, "landscape_path": str(landscape_path)}
        except Exception as e:
            logger.error(f"Visualization failed: {e}")
            return {"error": str(e)}

    async def _detect_node(self, state: ClusterState) -> dict:
        """检测异常值 → LLM 评估是否为新方向。"""
        papers = state.get("papers", [])
        embeddings = state.get("embeddings", [])
        cluster_papers = state.get("cluster_papers", {})
        await self._notify("新方向检测", 5, 5, "检测潜在新研究方向")

        outliers = []
        new_directions = []

        try:
            import numpy as np
            if len(embeddings) >= 10:
                X = np.array(embeddings)

                try:
                    from sklearn.cluster import HDBSCAN
                    hdb = HDBSCAN(min_cluster_size=3, min_samples=2)
                    hdb_labels = hdb.fit_predict(X)

                    outlier_indices = [i for i, lbl in enumerate(hdb_labels) if lbl == -1]
                    outliers = [
                        {"paper_id": papers[i].get("id", ""),
                         "title": papers[i].get("title", ""),
                         "reason": "HDBSCAN outlier — 不属于任何密度聚类"}
                        for i in outlier_indices if i < len(papers)
                    ]
                except ImportError:
                    # 基于质心距离的简单检测
                    centroids = {}
                    for lbl, cps in cluster_papers.items():
                        idxs = [i for i, p in enumerate(papers) if p.get("id") in [cp.get("id") for cp in cps]]
                        if idxs:
                            centroids[lbl] = X[idxs].mean(axis=0)

                    for i, p in enumerate(papers):
                        if centroids:
                            min_dist = min(np.linalg.norm(X[i] - c) for c in centroids.values())
                            threshold = np.percentile([np.linalg.norm(X[j] - list(centroids.values())[0])
                                                       for j in range(min(100, len(X)))], 90)
                            if min_dist > threshold:
                                outliers.append({
                                    "paper_id": p.get("id", ""),
                                    "title": p.get("title", ""),
                                    "reason": "Distance outlier — 远离所有聚类质心",
                                })

            # 如果有 outliers，LLM 评估是否构成新方向
            if outliers and self._llm and len(outliers) >= 3:
                outlier_titles = "\n".join(f"- {o['title'][:80]}" for o in outliers[:5])
                try:
                    llm_result = await self._llm.chat_json(
                        messages=[{"role": "user", "content": (
                            f"以下论文不属于主流研究方向聚类，请评估它们是否构成潜在的新研究方向：\n{outlier_titles}"
                        )}],
                        system="输出纯 JSON: {\"is_new_direction\": true, \"direction_name\": \"方向名\", \"confidence\": 0.7, \"rationale\": \"理由\"}",
                        node="gap_discovery",
                    )
                    if llm_result.get("is_new_direction"):
                        new_directions.append({
                            "name": llm_result.get("direction_name", "新兴方向"),
                            "confidence": llm_result.get("confidence", 0.5),
                            "rationale": llm_result.get("rationale", ""),
                            "papers": [o["paper_id"] for o in outliers],
                        })
                except Exception as e:
                                        logger.warning(f"LLM outlier detection failed: {e}")

        except Exception as e:
            logger.error(f"Outlier detection failed: {e}")

        # 保存聚类结果
        clusters_output = {
            "n_clusters": state.get("n_clusters", 0),
            "cluster_names": state.get("cluster_names", {}),
            "cluster_sizes": {str(k): len(v) for k, v in cluster_papers.items()},
            "outliers": outliers,
            "new_directions": new_directions,
        }

        from ...config import get_outputs_dir
        out_dir = get_outputs_dir() / (state.get("project_id", "default"))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "clusters.json").write_text(json.dumps(clusters_output, ensure_ascii=False, indent=2))

        return {
            "outliers": outliers,
            "new_directions": new_directions,
            "result": clusters_output,
        }

    # ── 辅助 ─────────────────────────────────────────

    async def _notify(self, stage: str, index: int, total: int, msg: str):
        logger.info(f"  Cluster [{index}/{total}] {stage}: {msg}")
        if self._on_progress:
            try:
                await self._on_progress(stage, index, total, 0, 0)
            except Exception as e:
                                logger.debug(f"ClusteringAgent on_progress error: {e}")
