# Paper Agent v4

> AI 科研助理 — 搜论文、下 PDF、读全文、写综述、知识库沉淀。

## 快速开始

```bash
docker compose up -d                   # 一键启动
DEPLOY_HOST=<IP> bash scripts/deploy.sh # 一键部署
```

## 架构

```
Vue WebUI / iOS
    │  WebSocket + REST
    ▼
API (:8000) ◄── Redis ───► MainAgent · Celery Worker/Beat
    │                           │
    └──────── PostgreSQL (pgvector) ──┘
```

## 文档

[AGENTS.md](AGENTS.md) — 开发 · [CLAUDE.md](CLAUDE.md) — 架构 · [docs/](docs/) — 设计

## License

MIT
