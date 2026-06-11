# 智驭·研 — Python 个人科研助理产品文档

**版本**: 1.0.0 | **日期**: 2026-06-11 | **语言**: Python 3.12+ | **部署**: Docker Compose

---

## 一、产品定位

**智驭·研** 是智驭系统的 Python 服务端——定位为**科研工作者的 AI 研究助理**。

在"双中心"架构中，iOS Agent 负责"日常助理"（设备控制、提醒、写作），Python Agent 负责"研究助理"（论文分析、视频理解、知识管理）。两者通过标准化 Tool API 通信，能力互补。

```
┌────────────────────────────────────────────┐
│              智驭 系统                       │
│                                             │
│  ┌─────────────┐     ┌──────────────────┐  │
│  │ iOS Agent   │←───→│ Python Agent     │  │
│  │ (日常助理)   │ API │ (研究助理)        │  │
│  │             │     │                  │  │
│  │ 16 Tools    │     │ 本服务           │  │
│  │ 设备/写作   │     │                  │  │
│  └─────────────┘     └──────────────────┘  │
└────────────────────────────────────────────┘
```

**核心价值**：让科研工作者用自然语言管理论文、消化视频、检索知识。AI 安全方向的研究者日常面对大量论文和技术视频——这个系统把"3 小时看完一篇论文 + 1 小时看完一个技术分享"压缩到"5 分钟获取结构化要点"。

---

## 二、用户画像与场景

### 2.1 主用户：AI 安全研究者

| 维度 | 描述 |
|------|------|
| 研究方向 | AI 安全 / 对抗攻击 / 模型鲁棒性 |
| 日常工作 | 读论文 → 复现实验 → 写论文 → 文献综述 |
| 核心痛点 | ① 论文太多，精读时间不够 ② 技术视频/课程「收藏即学会」③ 跨论文对比费时 |
| 使用频率 | 视频理解：每天遇到；论文分析：每周 3-5 篇 |

### 2.2 辅用户：全栈开发者

| 维度 | 描述 |
|------|------|
| 日常工作 | 技术文档 + 方案调研 + 代码开发 |
| 核心痛点 | 技术视频来不及看、文档检索效率低 |

### 2.3 核心使用场景

| 场景 | 输入 | 输出 | 耗时（vs 人工） |
|------|------|------|----------------|
| 论文速读 | PDF 上传 | 结构化卡片（方法/贡献/数据集/结果） | 1min vs 3h |
| 跨论文对比 | 两篇论文 ID | 对比表（方法差异/结果对比/贡献对比） | 2min vs 1h |
| 视频速读 | B站/YouTube 链接 | 文字稿 + 5 个关键要点 + 一句话总结 | 5min vs 1h |
| 知识库问答 | 自然语言问题 | 基于已存文档的 AI 回答 + 来源引用 | 实时 |
| 文献检索 | 关键词 | 本地知识库 + Semantic Scholar 联合结果 | 实时 |
| 每日摘要 | 自动触发 | AI 安全领域当日新论文/新闻汇总 | 自动 |

---

## 三、功能模块

### 3.1 模块一：RAG 知识库（增强版）

**定位**：所有知识的统一存储与检索中心。论文分析结果、视频文字稿、手动上传的文档统一存入，供 Agent 检索和问答。

| 功能 | 描述 | 状态 |
|------|------|------|
| 文档上传 | 支持 PDF / Markdown / TXT，拖拽上传或 API 调用 | ✅ |
| **层次化分块** | 文档→章节→段落→句子层级保留，检索可定位到具体段落 | 🆕 |
| **LLM 生成式问答** | /qa 端点增加 Claude API 调用，服务端直接返回完整答案（非仅返回 context） | 🆕 |
| 语义搜索 | 向量检索（ChromaDB / Milvus 双后端），返回片段 + 相似度分数 | ✅ |
| **Redis 查询缓存** | 热门搜索 embedding 缓存（TTL 1h），命中直接返回，避免重复推理 | 🆕 |
| **异步向量化** | 大文档向量化通过 Celery 异步执行，不阻塞 HTTP 请求 | 🆕 |

**技术要点**：
- 层次化分块：LangChain `MarkdownHeaderTextSplitter`（保留标题层级）+ `RecursiveCharacterTextSplitter`（段落内细分），chunk_size=800 / overlap=100
- 双后端：`VECTOR_STORE=chroma`（默认）/ `VECTOR_STORE=milvus`（环境变量切换），pymilvus 真实实现
- LLM 集成：/qa 调用 Claude API 基于检索上下文生成答案，含 token 用量统计

### 3.2 模块二：论文分析

**定位**：为 AI 安全研究者设计的论文结构化解析工具。

| 功能 | API | 描述 |
|------|-----|------|
| 论文解析 | `POST /api/papers/parse` | PDF 上传 → PyMuPDF 提取文本 → Claude API 结构化提取 |
| 论文搜索 | `POST /api/papers/search` | 本地向量搜索 + Semantic Scholar API 联合 |
| 论文对比 | `POST /api/papers/compare` | 两篇论文 LLM 对比分析（方法/贡献/实验/局限） |
| 论文详情 | `GET /api/papers/{id}` | 获取已索引论文的结构化数据 |
| 引用导出 | `POST /api/papers/export` | 导出 BibTeX / Markdown / APA 格式 |

**结构化提取字段**（LLM 从论文全文提取）：

```yaml
title: "Attention Is All You Need"
authors:
  - name: "Ashish Vaswani"
    affiliation: "Google Brain"
year: 2017
venue: "NeurIPS"
abstract: "..."
keywords: ["Transformer", "Self-Attention", "Machine Translation"]
methodology: "纯注意力机制替代 RNN/CNN 的序列到序列模型"
contributions:
  - "提出 Scaled Dot-Product Attention"
  - "提出 Multi-Head Attention"
  - "在 WMT 2014 英德翻译上达到 28.4 BLEU"
datasets: ["WMT 2014 English-German", "WMT 2014 English-French"]
baselines: ["GNMT+RL", "ConvS2S", "MoE"]
results_summary: "英德翻译 BLEU 28.4（SOTA），训练成本降低"
limitations: "位置编码的局限性，长序列 O(n²) 复杂度"
citation_count: 150000+
```

**为什么不是简单的"PDF 转文字"**：结构化提取让 LLM 理解论文的学术结构，而不是被动地 OCR。提取的字段可以直接用于跨论文对比——"这两篇论文用了同样的数据集吗？方法有什么本质区别？"

### 3.3 模块三：视频理解 Pipeline

**定位**：解决"大量技术视频/课程来不及看"的高频刚需。

**完整处理链路**：

```
用户分享链接（B站 / YouTube / 抖音 / ...）
        │
        ▼
POST /api/video/analyze  →  返回 task_id
        │
        ▼  (Celery 异步执行)
┌──────────────────────────────────────┐
│ 1. yt-dlp 下载视频                   │
│ 2. ffmpeg 提取音频 (16kHz mono wav)  │
│ 3. Faster-Whisper ASR → 文字稿       │
│ 4. 文字稿 → Claude API → 结构化摘要   │
│    ├── 标题/topic                    │
│    ├── 关键要点 (3-5)                 │
│    ├── 关键词                        │
│    └── 一句话总结                    │
│ 5. 文字稿 + 摘要存入向量知识库       │
│ 6. 视频文件 7天后自动清理            │
└──────────────────────────────────────┘
        │
        ▼
GET /api/video/status/{task_id}  →  返回结果
```

| API | 方法 | 功能 |
|-----|------|------|
| `/api/video/analyze` | POST | 提交视频链接 → 返回 task_id |
| `/api/video/status/{task_id}` | GET | 查询处理进度和结果 |
| `/api/video/transcript/{task_id}` | GET | 获取完整文字稿 |
| `/api/video/search` | POST | 搜索历史视频文字稿 |

**技术要点**：
- **Faster-Whisper 封装为微服务**：非脚本调用，FastAPI Web API 化，`ThreadPoolExecutor` 支持并发转写，`device` 参数支持 CPU/CUDA 自适应切换，FP16 不可用时自动降级 float32
- **yt-dlp**：覆盖 1000+ 网站，维护活跃，命令行封装
- **异步任务**：视频处理耗时长（下载+转写+摘要），全部通过 Celery 异步执行，iOS 端轮询状态
- **临时文件管理**：视频文件 7 天 TTL 自动清理，文字稿和摘要永久保留
- **ASR→RAG 串联**：语音转文字后自动向量化存入知识库，后续可被 RAG 问答检索

### 3.4 模块四：Daily Digest（每日 AI 安全摘要）

**定位**：每天自动推送 AI 安全领域最新动态。锦上添花的功能，1 天可完成。

| API | 功能 |
|-----|------|
| `POST /api/digest` | 手动触发一次摘要生成 |
| `GET /api/digest/latest` | 获取最新一次摘要 |

通过 Celery Beat 定时任务，每天 8:00 AM 自动执行。

---

## 四、技术架构

### 4.1 系统架构图

```
┌─────────────────────────────────────────────────────┐
│                   Nginx (:80)                        │
│                  反向代理                             │
└────────┬────────────────────┬───────────────────────┘
         │                    │
         ▼                    ▼
┌─────────────────┐  ┌──────────────────────┐
│  FastAPI (:8000) │  │  Flower (:5555)       │
│                  │  │  Celery 监控面板       │
│  /api/search     │  └──────────────────────┘
│  /api/qa         │
│  /api/documents/*│  ┌──────────────────────┐
│  /api/papers/*   │  │  Celery Worker        │
│  /api/video/*    │  │  (异步任务执行)        │
│  /api/digest     │  │  - 文档向量化          │
│  /health         │  │  - 视频下载+ASR+摘要   │
└────────┬─────────┘  │  - 论文解析            │
         │            │  - Daily Digest        │
         ▼            └──────────┬─────────────┘
┌─────────────────┐              │
│  Redis (:6379)   │◄────────────┘
│  - 查询缓存      │  (Celery Broker + Backend)
│  - 任务队列      │
│  - 会话状态      │
└─────────────────┘

┌─────────────────┐  ┌──────────────────────┐
│  ChromaDB        │  │  Milvus (可选)        │
│  (默认向量存储)   │  │  (企业级向量存储)      │
└─────────────────┘  └──────────────────────┘

┌─────────────────┐
│  GPU Server      │  (AutoDL RTX 3090)
│  - Faster-Whisper│
│  - Embedding     │
└─────────────────┘
```

### 4.2 并发与异步模型

| 层级 | 模型 | 说明 |
|------|------|------|
| Web 层 | FastAPI async/await | 异步 HTTP 处理 |
| CPU 密集型 | Celery Worker | 文档向量化、视频转写等长时间任务 |
| GPU 推理 | ThreadPoolExecutor | Faster-Whisper 并发转写 |
| 定时任务 | Celery Beat | Daily Digest、临时文件清理 |
| 缓存 | Redis | 查询缓存 TTL 1h、任务状态存储 |

### 4.3 模型部署方案

| 能力 | 使用的模型 | 部署方式 | 理由 |
|------|-----------|---------|------|
| 文本向量化 | sentence-transformers (all-MiniLM-L6-v2) | CPU 服务器常驻 | 轻量，无需 GPU |
| 语音转文字 | Faster-Whisper (small/medium) | AutoDL GPU 按量付费 | 需 GPU 推理，按需使用 |
| 文本生成 | Claude API (claude-sonnet-4-6) | 远程 API | 质量最好，无需自部署 |
| (可选)本地 LLM | Qwen2.5-7B + vLLM | AutoDL GPU 常驻 | 可选，隐私/成本考虑 |
| Embedding 优化 | ONNX Runtime | CPU 服务器 | 推理加速 1.5-2x |

**GPU 成本**：AutoDL RTX 3090 约 1.5 元/时。一个 1 小时视频转写约需 5-10 分钟 GPU 时间（~0.25 元）。日常使用月均 30-50 元。

### 4.4 向量数据库双方案

| 维度 | ChromaDB | Milvus |
|------|----------|--------|
| **部署** | 嵌入式，无需额外容器 | Docker（etcd + MinIO + Milvus） |
| **启动速度** | 即时 | ~30 秒 |
| **适合规模** | < 100K 文档 | > 100K 文档 |
| **检索速度** | ~10ms | ~2ms (IVF_FLAT 索引) |
| **资源占用** | 共享 Python 进程内存 | 3 容器 ~2GB RAM |
| **适用场景** | 个人科研知识库（Demo/原型） | 实验室/团队共享知识库 |
| **切换方式** | `VECTOR_STORE=chroma` (默认) | `VECTOR_STORE=milvus` |

---

## 五、API 全景

### 5.1 知识库 API (`/api/documents/*`)

| 方法 | 路径 | 功能 | 认证 |
|------|------|------|------|
| `POST` | `/api/documents/upload` | 上传文本内容并向量化 | - |
| `POST` | `/api/documents/upload/file` | 上传文件 (PDF/MD/TXT) 并向量化 | - |
| `GET` | `/api/documents/list` | 向量存储统计 | - |
| `DELETE` | `/api/documents/{doc_id}` | 删除文档及所有分块 | - |

### 5.2 检索与问答 API (`/api/*`)

| 方法 | 路径 | 功能 | 特殊 |
|------|------|------|------|
| `POST` | `/api/search` | 语义搜索 | Redis 缓存 TTL 1h |
| `POST` | `/api/qa` | RAG 问答（LLM 生成） | 调用 Claude API |

### 5.3 论文分析 API (`/api/papers/*`)

| 方法 | 路径 | 功能 |
|------|------|------|
| `POST` | `/api/papers/parse` | PDF 上传 → 结构化提取 |
| `GET` | `/api/papers/{id}` | 获取论文详情 |
| `POST` | `/api/papers/search` | 本地 + Semantic Scholar 联合搜索 |
| `POST` | `/api/papers/compare` | 两篇论文 LLM 对比分析 |
| `POST` | `/api/papers/export` | 导出引用 (BibTeX/Markdown/APA) |

### 5.4 视频理解 API (`/api/video/*`)

| 方法 | 路径 | 功能 |
|------|------|------|
| `POST` | `/api/video/analyze` | 提交链接 → 异步任务 |
| `GET` | `/api/video/status/{task_id}` | 查询任务进度 |
| `GET` | `/api/video/transcript/{task_id}` | 获取完整文字稿 |
| `POST` | `/api/video/search` | 搜索历史文字稿 |

### 5.5 Digest API (`/api/digest`)

| 方法 | 路径 | 功能 |
|------|------|------|
| `POST` | `/api/digest` | 手动触发当日摘要 |
| `GET` | `/api/digest/latest` | 获取最新摘要 |

### 5.6 系统 API

| 方法 | 路径 | 功能 |
|------|------|------|
| `GET` | `/health` | 健康检查 + 服务状态 |
| `GET` | `/docs` | Swagger 自动 API 文档 |

---

## 六、与 iOS Agent 的协作

### 6.1 通信协议

iOS Agent 通过 `CallServerAPI` Tool 或专用 Tool 调用 Python 服务端 API：

```
iOS Agent (Swift)                  Python Agent (FastAPI)
    │                                    │
    │  POST /api/papers/parse            │
    │  ─────────────────────────────────> │
    │  Content: PDF binary               │  PyMuPDF 提取文本
    │                                    │  Claude API 结构化提取
    │  <───────────────────────────────── │
    │  PaperMetadata JSON                 │
    │                                    │
    │  "这篇论文用了什么数据集？"          │
    │  ─────────────────────────────────> │
    │  POST /api/qa                      │
    │  {question: "...", top_k: 5}       │  向量检索 → LLM 生成
    │  <───────────────────────────────── │
    │  {answer: "WMT 2014...", sources}  │
```

### 6.2 iOS Tool 映射

| iOS Agent Tool | 调用的 Python API | 典型对话 |
|---------------|-------------------|---------|
| `QueryKnowledge` | `/api/search` | "帮我找一下 Transformer 相关的论文" |
| `UploadDocument` | `/api/documents/upload` | "把这篇论文存到知识库" |
| `AnalyzePaper` | `/api/papers/parse` | "分析这篇 Attention Is All You Need" |
| `ComparePapers` | `/api/papers/compare` | "对比 Transformer 和 BERT 的方法差异" |
| `VideoToText` | `/api/video/analyze` | "帮我把这个 B站 视频转成文字稿" |
| `DailyDigest` | `/api/digest` | "今天 AI 安全领域有什么新动态" |
| `CallServerAPI` | 通用 | 用于上面未覆盖的 API 调用 |

### 6.3 分工原则

| 任务 | 在哪执行 | 理由 |
|------|---------|------|
| 设备控制（文件/位置/通知/提醒） | iOS 本地 | 隐私 + iOS 原生 API |
| AI 写作（写作/翻译/润色/代码） | iOS 本地 | 轻量 LLM API 调用 |
| STT（实时语音输入） | iOS 本地 | Speech 框架 |
| TTS（语音朗读） | iOS 本地 | AVSpeechSynthesizer |
| **论文结构化解析** | **Python 服务端** | PyMuPDF + GPU 不可用 |
| **视频下载 + ASR** | **Python 服务端** | yt-dlp + Faster-Whisper GPU |
| **向量搜索** | **Python 服务端** | Chroma/Milvus |
| **长时间异步任务** | **Python 服务端** | Celery + iOS 后台限制 |
| **批量文档处理** | **Python 服务端** | ETL 管道式处理 |

---

## 七、部署方案

### 7.1 一键启动

```bash
# 默认（ChromaDB 后端）
docker-compose up -d

# 含 Milvus
docker-compose -f docker-compose.yml -f docker-compose.milvus.yml up -d
```

启动后服务：

| 服务 | 端口 | 用途 |
|------|------|------|
| rag-api | 8000 | FastAPI 主服务 |
| celery-worker | - | 异步任务执行 |
| celery-beat | - | 定时任务调度 |
| flower | 5555 | Celery 监控面板 |
| redis | 6379 | 缓存 + 任务队列 |
| nginx | 80 | 反向代理 |

### 7.2 GPU 服务器配置

```bash
# AutoDL 服务器启动（独立于 Docker，直连 GPU）
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Celery Worker（GPU 密集型任务用）
celery -A app.celery_app worker --loglevel=info --concurrency=1
```

### 7.3 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EMBEDDING_MODEL` | all-MiniLM-L6-v2 | 向量化模型 |
| `CHROMA_PATH` | ./data/chroma | ChromaDB 存储路径 |
| `VECTOR_STORE` | chroma | chroma / milvus |
| `MILVUS_HOST` | localhost | Milvus 地址 |
| `USE_ONNX` | false | ONNX Runtime 优化 |
| `REDIS_URL` | redis://localhost:6379 | Redis 连接 |
| `ANTHROPIC_API_KEY` | - | Claude API Key |
| `LLM_MODEL` | claude-sonnet-4-6 | LLM 模型名 |
| `CHUNK_SIZE` | 800 | 文档分块大小 |
| `TEMP_DIR` | ./data/temp | 临时文件目录 |

---

## 八、技术栈与 JD 技能映射

### 8.1 技术栈总览

| 层级 | 技术 | 用途 |
|------|------|------|
| Web 框架 | FastAPI | 异步 REST API，Swagger 自动文档 |
| 异步任务 | Celery + Redis | 长时间任务编排 |
| 向量数据库 | ChromaDB + Milvus | 双后端，真实切换 |
| 文档处理 | LangChain Text Splitters + PyMuPDF | 层次化分块 + PDF 解析 |
| 语音转写 | Faster-Whisper (CTranslate2) | GPU 推理，Web API 封装 |
| 视频下载 | yt-dlp | 1000+ 网站支持 |
| AI 模型 | Claude API + sentence-transformers + ONNX Runtime | 生成 / 向量化 / 推理优化 |
| 部署 | Docker Compose + Nginx | 一键部署 |
| GPU | AutoDL RTX 3090 | 按量付费 |
| 监控 | Flower | Celery 任务监控 |

### 8.2 JD 技能映射矩阵

| JD 技能 | 频率 | 本项目体现 | 面试话术关键词 |
|---------|------|-----------|--------------|
| **Python 精通** | 🔴必备 | FastAPI 异步 + Celery 任务编排 + Faster-Whisper 微服务 + yt-dlp 集成 + 论文解析 Pipeline | "不只是写脚本，是完整的后端系统" |
| **Docker/Docker-Compose** | 🔴必备 | 6 容器编排（API+Worker+Beat+Flower+Redis+Nginx）+ Milvus 可选 | "生产级多容器编排" |
| **AI 模型训练+推理** | 🔴必备 | Faster-Whisper GPU 推理 + sentence-transformers + ONNX Runtime | "GPU 推理部署实战" |
| **FastAPI/Flask** | 🟡高频 | 5 个 Router、30+ 端点、Pydantic Models、async/await | "完整 RESTful API 设计" |
| **LLM 应用 (RAG/Agent/Prompt)** | 🟡高频 | RAG 全链路（层次化分块→向量化→检索→LLM生成）+ ASR→RAG 串联 | "全链路 RAG，不只是调 API" |
| **LangChain** | 🟡高频 | Document Loaders + Text Splitters + 自建 iOS LangChain 对比 | "源码级理解，跨语言移植" |
| **ONNX/模型部署** | 🟡高频 | ONNX Runtime Embedding 推理优化 | "实际集成+性能对比" |
| **向量数据库** | 🟢加分 | Chroma + Milvus 双方案真实实现+性能对比 | "双后端切换，有真实数据" |
| **SQL/数据库** | 🟡高频 | Redis + ChromaDB + Milvus | "3 种存储引擎实战" |
| **Linux 基础运维** | 🟡高频 | Nginx 反向代理 + GPU 服务器 + Celery 监控 | "生产环境运维" |
| **数据治理/ETL** | 🟡高频 | 视频→音频→文字稿→摘要→向量化 完整管道 | "多模态数据 ETL 管道" |
| **需求拆解** | 🔵软技能 | 双中心架构 + 模块化设计 + Dify 对比 | "从需求到架构的完整思维" |
| **文档能力** | 🔵软技能 | API 文档 + 架构文档 + 性能对比 + 本产品文档 | "18+ 份技术文档" |

---

## 九、面试 Demo 场景

### 9.1 核心演示：双 Agent 协同（2 分钟）

```
1. 打开智驭 App → 本地 Agent Tab
   "帮我分析这篇 Attention Is All You Need 论文"
   → iOS Agent 调用 AnalyzePaper Tool
   → Python 服务端解析 PDF → 返回结构化卡片

2. 追问
   "这篇和 BERT 论文比有什么不同？"
   → ComparePapers Tool → Python LLM 对比分析 → 返回对比表

3. 切换场景
   "帮我分析这个 B站 技术分享"
   → VideoToText Tool → yt-dlp 下载 → Faster-Whisper 转写
   → Claude 摘要 → 返回文字稿 + 5 个要点

4. 知识沉淀
   "把论文和视频摘要都存到知识库"
   → UploadDocument → 向量化存储

5. 验证
   "Transformer 里 Multi-Head Attention 怎么实现的？"
   → /qa → RAG 检索 → Claude 基于上下文的精准回答
```

### 9.2 面试口述要点

> **开场**：
> "这个 Python 项目是我'双中心 AI 架构'的服务端——iOS 端 Agent 负责日常设备操作，Python 端 Agent 负责研究助理。目前有 4 个模块、30+ 个 API 端点、6 个 Docker 服务。"

> **RAG**：
> "不是简单的全文向量化。我做了层次化分块——保留文档的章节→段落→句子层级，检索时可以定位到具体段落。双后端切换——Chroma 日常用，Milvus 上生产。Redis 查询缓存，Celery 异步向量化。"

> **论文分析**：
> "不只是 PDF 转文字。我用 Claude API 做了结构化提取——自动识别方法、贡献、数据集、baseline、局限性。11 个字段，格式统一。这样后面跨论文对比、写文献综述都很方便。"

> **视频 Pipeline**：
> "一个完整的 ETL 管道。yt-dlp 下载 → ffmpeg 提取音频 → Faster-Whisper GPU 转写 → Claude 摘要。全部 Celery 异步执行。Faster-Whisper 我封装成了 FastAPI 微服务，不是调脚本。"

> **对面试官**：
> "这套系统证明了：① Python 工程能力——从 API 设计到异步任务到容器部署；② AI 应用落地能力——不是 Demo 是真正能用的；③ 需求拆解能力——从科研痛点出发设计架构。"

---

## 十、开发路线图

| 阶段 | 内容 | 工期 |
|------|------|------|
| Phase A | RAG 增强（层次化分块 + LLM 集成 + Milvus + Redis 缓存） | 1-2 天 |
| Phase B | 论文分析模块（Router + PDF 解析 + Semantic Scholar） | 2-3 天 |
| Phase C | 视频理解 Pipeline（yt-dlp + Faster-Whisper + Celery 异步） | 3-4 天 |
| Phase D | Daily Digest + Celery Beat + Dify 对比文档 | 1-2 天 |
| **总计** | | **7-11 天** |

---

*本文档 2026-06-11 初稿。随项目迭代持续更新。*
