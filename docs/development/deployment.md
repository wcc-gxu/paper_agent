# Paper Agent v3 — 部署与运维文档

> Docker + docker-compose 部署方案 | 2026-06-14

---

## 1. Docker Compose 架构

```
docker-compose.yml
├── agent          — Agent 守护进程 (FastAPI + WebSocket)
├── celery_worker  — Celery Worker (异步长任务)
├── celery_beat    — Celery Beat (定时任务调度)
└── redis          — 消息队列 + 事件总线 + Celery Broker
```

---

## 2. Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[all]" && \
    pip install --no-cache-dir \
    langchain-core langchain-openai langgraph langchain-community \
    tiktoken sentence-transformers \
    redis celery[redis] \
    fastapi uvicorn[standard]

# 应用代码
COPY src/ src/

# 数据目录
RUN mkdir -p /papers/outputs /papers/markdown /papers/pdfs /root/.paper_search/logs/tasks

EXPOSE 8000

CMD ["python", "-m", "paper_search.agent.daemon"]
```

---

## 3. docker-compose.yml

```yaml
version: "3.8"

services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --appendonly yes --appendfsync everysec --save 900 1
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s

  celery_worker:
    build: .
    restart: unless-stopped
    command: celery -A paper_search.agent.celery_app worker --loglevel=info --concurrency=4
    env_file: .env
    volumes:
      - papers_data:/papers
      - agent_data:/root/.paper_search
    depends_on:
      redis:
        condition: service_healthy

  celery_beat:
    build: .
    restart: unless-stopped
    command: celery -A paper_search.agent.celery_app beat --loglevel=info
    env_file: .env
    volumes:
      - agent_data:/root/.paper_search
    depends_on:
      redis:
        condition: service_healthy

  agent:
    build: .
    restart: unless-stopped
    ports:
      - "8000:8000"
    env_file: .env
    volumes:
      - papers_data:/papers
      - agent_data:/root/.paper_search
    depends_on:
      - celery_worker
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      retries: 3

volumes:
  redis_data:
  papers_data:
  agent_data:
```

---

## 4. 环境变量 (.env)

```bash
# ── LLM ─────────────────────────────────
VOLCANO_API_KEY=your_key
OPENAI_API_KEY=          # 可选
ANTHROPIC_API_KEY=       # 可选
LLM_DEFAULT_PROVIDER=volcano
LLM_DEFAULT_MODEL=deepseek-v4-pro

# ── 搜索 API ────────────────────────────
SEMANTIC_SCHOLAR_API_KEY=your_key
ELSEVIER_API_KEY=your_key
IEEE_API_KEY=your_key

# ── Redis ───────────────────────────────
REDIS_URL=redis://redis:6379/0

# ── 存储路径 ─────────────────────────────
PAPERS_DIR=/papers/pdfs
MARKDOWN_DIR=/papers/markdown
OUTPUTS_DIR=/papers/outputs
AGENT_DATA_DIR=/root/.paper_search

# ── Agent 配置 ───────────────────────────
AGENT_MAX_STEPS=50
AGENT_MAX_AUTO_RETRIES=2
AGENT_SHORT_TERM_MAX_TOKENS=8000
AGENT_IOS_TOOL_DEFAULT_TIMEOUT=30

# ── Celery 配置 ──────────────────────────
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2

# ── 日志 ─────────────────────────────────
LOG_LEVEL=INFO
LOG_FILE=/root/.paper_search/logs/agent.log
TASK_LOG_DIR=/root/.paper_search/logs/tasks
LOG_MAX_DAYS=30

# ── iOS 推送 ─────────────────────────────
APNS_KEY_PATH=/app/certs/apns.p8
APNS_KEY_ID=ABC123
APNS_TEAM_ID=DEF456
APNS_TOPIC=com.your.app
```

---

## 5. 启动命令

```bash
# 构建并启动全部服务
docker-compose up -d

# 查看日志
docker-compose logs -f agent

# 停止
docker-compose down

# 完全清理（含数据卷）
docker-compose down -v
```

---

## 6. 健康检查

### 6.1 /health 端点

```json
{
  "status": "healthy",
  "version": "3.0.0",
  "uptime_seconds": 86400,
  "db": {
    "documents": 1523,
    "projects": 5,
    "size_mb": 45
  },
  "chromadb": {
    "collections": 5,
    "total_embeddings": 8912
  },
  "redis": {
    "status": "connected",
    "event_queue_length": 3
  },
  "celery": {
    "workers": 4,
    "active_tasks": 1,
    "scheduled_tasks": 3
  },
  "providers": {
    "arxiv": true,
    "semantic_scholar": true,
    "pubmed": true
  },
  "llm": {
    "provider": "volcano",
    "status": "ok",
    "rpm_remaining": 45
  }
}
```

### 6.2 自动健康检查

Celery Beat 每 20 分钟：
- 检查所有 Provider 可用性
- 检查 LLM 余额/速率限制
- 检查 ChromaDB 索引一致性
- 异常 → 写日志 → 必要时 APNs 通知用户

---

## 7. 日志管理

### 7.1 Agent 全局日志

```
~/.paper_search/logs/agent.log          (每天 rotate, keep 30 天)

格式（结构化 JSONL，每行一个 JSON 对象）:
{"ts":"2026-06-14T15:30:00Z","event":"task_launched","task_id":"task-001","project_id":"proj-xxx"}
{"ts":"2026-06-14T15:30:05Z","event":"tool_call","tool":"download_paper","paper_id":"paper-001","status":"start"}
{"ts":"2026-06-14T15:30:10Z","event":"tool_call","tool":"download_paper","paper_id":"paper-001","status":"done"}
{"ts":"2026-06-14T15:30:10Z","event":"health","provider":"ieee","status":"unavailable","level":"warn"}
{"ts":"2026-06-14T15:31:00Z","event":"memory","action":"compress","before_tokens":8000,"after_tokens":3500}
```

### 7.2 Task 独立日志（每个子Agent 入库任务一个文件）

```
~/.paper_search/logs/tasks/task-{YYYYMMDD}-{序号}.jsonl

格式（结构化 JSONL，7 种事件类型）:
{"ts":"...","event":"task_start","task_id":"...","project_id":"...","plan":{...}}
{"ts":"...","event":"stage_start","task_id":"...","stage":"search","stage_index":1,"total_stages":7}
{"ts":"...","event":"stage_progress","task_id":"...","stage":"search","current":10,"total":50}
{"ts":"...","event":"paper_progress","task_id":"...","stage":"download","paper_id":"...","title":"...","event_type":"download_done"}
{"ts":"...","event":"stage_done","task_id":"...","stage":"search","result":{...}}
{"ts":"...","event":"task_done","task_id":"...","result":{...}}
{"ts":"...","event":"task_error","task_id":"...","error":"...","traceback":"..."}
```

每个 `paper_progress` 事件的 `event_type`：`search_found` / `eval_complete` / `download_start|done|failed|skip` / `convert_start|done|failed|skip` / `index_start|done|failed` / `rank_done`

### 7.3 LLM 决策审计

所有 LLM 做出的关键决策记录到日志：
- 为什么选择某个关键词
- 为什么重试（LLM 的 reasoning）
- 为什么删除某些记忆

---

## 8. 监控建议（非 MVP）

| 指标 | 方式 |
|------|------|
| Agent 进程存活 | Docker HEALTHCHECK |
| Redis 队列积压 | `redis-cli LLEN agent:events` |
| Celery 任务积压 | Flower 监控面板 (可选) |
| LLM 调用量 | 日志统计 |
| 磁盘使用 | `df -h /papers` |

---

## 9. 数据备份

```bash
# SQLite 备份
cp ~/.paper_search/agent.db ~/backups/agent_$(date +%Y%m%d).db

# ChromaDB 备份
cp -r ~/.paper_search/chroma/ ~/backups/chroma_$(date +%Y%m%d)/

# 论文文件备份 (可通过 rsync)
rsync -av /papers/ ~/backups/papers/
```

---

> 版本: v1.2 | Redis AOF 持久化 + 多供应商 Embedding 可插拔
