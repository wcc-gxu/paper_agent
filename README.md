# Paper Agent v4

> AI 科研助理 — 搜论文、下 PDF、读全文、写综述、知识库沉淀。

## 快速开始

```bash
docker compose up -d                    # 本地启动
# 部署
DEPLOY_HOST=<IP> bash scripts/deploy.sh                   # 生产
DEPLOY_HOST=<IP> DEPLOY_ENV=test bash scripts/deploy.sh    # 测试
```

## Git Flow + CI/CD

```
feature/* ──→ dev ──→ master
    CI: test    CI: test+build→测试机    CI: test+build→手动部署生产
```

[AGENTS.md](AGENTS.md) — 开发 · [CLAUDE.md](CLAUDE.md) — 架构 · [docs/](docs/) — 设计

## License

MIT
