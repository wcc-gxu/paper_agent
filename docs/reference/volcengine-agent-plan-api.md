# 火山方舟 Agent Plan / Coding Plan API 参考

> 来源：火山引擎官方文档 | 采集日期：2026-06-15
> 对应页面：https://www.volcengine.com/docs/82379/2375464（Agent Plan API）
> 相关页面：记忆增强-Embedding模型 (docs/82379/2279748)、Coding Plan 个人版 (docs/82379/1925115)

---

## 1. 套餐概览

| 套餐 | Base URL (Anthropic) | Base URL (OpenAI) | 说明 |
|------|---------------------|-------------------|------|
| **Agent Plan** | `https://ark.cn-beijing.volces.com/api/plan` | `https://ark.cn-beijing.volces.com/api/plan/v3` | 智能体场景，含 Embedding |
| **Coding Plan Lite** | `https://ark.cn-beijing.volces.com/api/coding` | `https://ark.cn-beijing.volces.com/api/coding/v3` | 编码场景 Lite |
| **Coding Plan Pro** | `https://ark.cn-beijing.volces.com/api/coding` | `https://ark.cn-beijing.volces.com/api/coding/v3` | 编码场景 Pro |

> ⚠️ **重要警告**：不要使用 `https://ark.cn-beijing.volces.com/api/v3`，该端点**不会消耗 Plan 额度**，产生额外按量计费。

---

## 2. 支持的模型

### LLM 模型
`doubao-seed-2.0-code`、`doubao-seed-2.0-pro`、`doubao-seed-2.0-lite`、`doubao-seed-code`、`minimax-m2.7`、`minimax-m2.5`、`glm-5.1`、`glm-4.7`、`deepseek-v3.2`、`kimi-k2.6`、`kimi-k2.5` 等。

### Embedding 模型
| 模型 | 维度 | 版本 | 说明 |
|------|------|------|------|
| `doubao-embedding-vision` | 1024 | 251215（最新）| 多模态（文本+图片+视频），支持 instructions 字段 |
| `doubao-embedding-vision` | 1024 | 250615 | 上一版本，支持稀疏向量 |

---

## 3. Embedding API 调用

### OpenAI 兼容格式（通过 Coding Plan v3 端点）

```python
import requests

url = "https://ark.cn-beijing.volces.com/api/coding/v3/embeddings"
headers = {
    "Authorization": "Bearer YOUR_ARK_API_KEY",
    "Content-Type": "application/json"
}
data = {
    "model": "doubao-embedding-vision",
    "input": "需要向量化的文本",
    "encoding_format": "float"
}

response = requests.post(url, headers=headers, json=data)
vectors = response.json()["data"]
```

### OpenClaw 配置示例

```json
{
  "agents": {
    "defaults": {
      "memorySearch": {
        "provider": "openai",
        "model": "doubao-embedding-vision",
        "remote": {
          "baseUrl": "https://ark.cn-beijing.volces.com/api/coding/v3",
          "apiKey": "<ARK_API_KEY>"
        }
      }
    }
  }
}
```

---

## 4. Claude Code 接入 Agent Plan

### 配置文件

`~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "<ARK_API_KEY>",
    "ANTHROPIC_BASE_URL": "https://ark.cn-beijing.volces.com/api/plan",
    "ANTHROPIC_MODEL": "<MODEL_NAME>"
  }
}
```

`~/.claude.json`:
```json
{ "hasCompletedOnboarding": true }
```

### 一键配置工具 (Ark Helper)

```bash
curl -fsSL https://lf3-static.bytednsdoc.com/obj/eden-cn/ylwslo-yrh/ljhwZthlaukjlkulzlp/install.sh | sh
ark-helper
```

---

## 5. 我们的项目配置

### LLM (Chat Completions)
```
Base URL: https://ark.cn-beijing.volces.com/api/plan/v3
协议: OpenAI-compatible (/v1/chat/completions)
API Key: 火山引擎 ARK_API_KEY (同 .env 中 VOLCANO_API_KEY)
```

### Embedding
```
Base URL: https://ark.cn-beijing.volces.com/api/plan/v3
协议: OpenAI-compatible (/v1/embeddings)
模型: doubao-embedding-vision
维度: 1024
API Key: 同 LLM

注意: 需验证 Agent Plan 的 /v3 端点是否路由 /embeddings 请求。
      如果不能，则需要单独配置 Embedding 端点。
```

### 多供应商可插拔设计

```python
# 配置示例（可切换供应商）
EMBEDDING_CONFIGS = {
    "volcano": {
        "model": "doubao-embedding-vision",
        "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
    },
    "deepseek": {
        "model": "deepseek-embedding-v1",
        "base_url": "https://api.deepseek.com/v1",
    },
    "openai": {
        "model": "text-embedding-3-small",
        "base_url": "https://api.openai.com/v1",
    },
}
```

---

> 版本: v1.0 | 基于火山引擎公开文档 + 搜索交叉验证
