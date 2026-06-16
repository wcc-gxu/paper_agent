# 火山引擎联网搜索 API 调用指南

> 基于 byted-web-search skill v1.3.4 逆向分析 + 官方文档
> 官方主文档：https://www.volcengine.com/docs/85508/1650263
> 官方 API 参考：https://www.volcengine.com/docs/87772/2272953

---

## 1. 概述

火山引擎联网搜索 API 提供网页搜索和图片搜索能力。个人用户每月自动获得 **500 次免费额度**。

### 与火山方舟 (Ark) 联网搜索的区别

| 项目 | 联网搜索 API（本文档） | 方舟 Ark 联网搜索 |
|------|----------------------|-------------------|
| 控制台 | [search-infinity](https://console.volcengine.com/search-infinity/web-search) | [Ark 控制台](https://console.volcengine.com/ark/) |
| API Key | 联网搜索控制台签发 | Ark 控制台签发 |
| 凭证格式 | `Bearer <key>` | 通过大模型 `tools` 参数传入 |
| 文档 | docs/85508/1650263 | docs/82379/1756990 |
| **凭证不通用** | ⚠️ | ⚠️ |

---

## 2. 认证方式

### 2.1 方式一：API Key（推荐，最简单）

```python
import requests

url = "https://open.feedcoopapi.com/search_api/web_search"
headers = {
    "Content-Type": "application/json",
    "X-Traffic-Tag": "skill_web_search_common",
    "Authorization": "Bearer <WEB_SEARCH_API_KEY>",
}
response = requests.post(url, headers=headers, json=body, timeout=30)
```

**获取 API Key**：
1. 打开 [联网搜索控制台](https://console.volcengine.com/search-infinity/web-search) →【正式开通】
2. 进入 [API Key 管理](https://console.volcengine.com/search-infinity/api-key) →【创建 API Key】
3. 复制保存

**配置方式**（优先级从高到低）：
1. CLI 参数：`--api-key <key>`
2. 环境变量：`export WEB_SEARCH_API_KEY="<key>"`
3. 项目 `.env` 文件：`WEB_SEARCH_API_KEY=<key>`

> 本项目已配置 `.env`，开箱即用。

### 2.2 方式二：AK/SK（HMAC-SHA256 签名）

```python
import hashlib, hmac, datetime
from urllib.parse import quote

# ---- 常量 ----
SERVICE = "volc_torchlight_api"
VERSION = "2025-01-01"
REGION = "cn-beijing"
HOST = "mercury.volcengineapi.com"
ACTION = "WebSearch"

# ---- 签名函数 ----
def _hmac_sha256(key: bytes, content: str) -> bytes:
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()

def _hash_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

def _norm_query(params: dict) -> str:
    query = ""
    for key in sorted(params.keys()):
        if isinstance(params[key], list):
            for v in params[key]:
                query += quote(key, safe="-_.~") + "=" + quote(v, safe="-_.~") + "&"
        else:
            query += quote(key, safe="-_.~") + "=" + quote(str(params[key]), safe="-_.~") + "&"
    return query[:-1].replace("+", "%20") if query else ""

def sign_request(ak: str, sk: str, body: str) -> dict:
    """火山引擎 OpenAPI HMAC-SHA256 签名"""
    now = datetime.datetime.now(datetime.timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    x_content_sha256 = _hash_sha256(body)

    # 构建规范请求
    canonical_request = "\n".join([
        "POST",
        "/",
        _norm_query({"Action": ACTION, "Version": VERSION}),
        f"content-type:application/json",
        f"host:{HOST}",
        f"x-content-sha256:{x_content_sha256}",
        f"x-date:{x_date}",
        f"x-traffic-tag:skill_web_search_common",
        "",
        "content-type;host;x-content-sha256;x-date;x-traffic-tag",
        x_content_sha256,
    ])

    # 签名
    credential_scope = f"{short_date}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        x_date,
        credential_scope,
        _hash_sha256(canonical_request),
    ])

    k_date = _hmac_sha256(sk.encode("utf-8"), short_date)
    k_region = _hmac_sha256(k_date, REGION)
    k_service = _hmac_sha256(k_region, SERVICE)
    k_signing = _hmac_sha256(k_service, "request")
    signature = _hmac_sha256(k_signing, string_to_sign).hex()

    authorization = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders=content-type;host;x-content-sha256;x-date;x-traffic-tag, "
        f"Signature={signature}"
    )

    return {
        "Content-Type": "application/json",
        "Host": HOST,
        "X-Date": x_date,
        "X-Content-Sha256": x_content_sha256,
        "X-Traffic-Tag": "skill_web_search_common",
        "Authorization": authorization,
    }

# 使用
url = f"https://{HOST}?Action={ACTION}&Version={VERSION}"
headers = sign_request(ak, sk, json.dumps(body))
response = requests.post(url, headers=headers, data=body_str.encode("utf-8"), timeout=30)
```

**AK/SK 获取**：控制台头像 → API 访问密钥 → 创建密钥（SK 仅显示一次！）

---

## 3. 请求体结构

### 3.1 完整请求体 Schema

```json
{
  "Query": "string (必填，1~100 字符)",
  "SearchType": "string (可选，默认 'web'，可选值: 'web' | 'image')",
  "Count": "integer (可选，默认 10，web ≤ 50，image ≤ 5)",
  "NeedSummary": "boolean (可选，仅 web 支持，建议设为 true)",
  "TimeRange": "string (可选，枚举值或日期区间)",
  "Filter": {
    "AuthInfoLevel": "integer (可选，0=全部，1=仅权威来源)"
  },
  "QueryControl": {
    "QueryRewrite": "boolean (可选，开启查询改写优化)"
  }
}
```

### 3.2 最小请求示例

```json
{
  "Query": "大模型最新进展",
  "SearchType": "web",
  "Count": 10
}
```

### 3.3 完整请求示例

```json
{
  "Query": "transformer attention mechanism",
  "SearchType": "web",
  "Count": 20,
  "NeedSummary": true,
  "TimeRange": "2025-01-01..2026-06-16",
  "Filter": {
    "AuthInfoLevel": 0
  },
  "QueryControl": {
    "QueryRewrite": true
  }
}
```

---

## 4. 参数详解

### 4.1 搜索类型 (`SearchType`)

| 值 | 说明 | 最大 Count | 返回字段 |
|----|------|-----------|---------|
| `web` | 网页搜索（默认） | 50 | Title, Url, SiteName, Summary, AuthInfoDes, SortId |
| `image` | 图片搜索 | 5 | Title, Image.Url, Image.Width, Image.Height, Image.Shape |

### 4.2 时间范围 (`TimeRange`)

**快捷枚举值**：
| 值 | 含义 |
|----|------|
| `OneDay` | 最近 1 天 |
| `OneWeek` | 最近 1 周 |
| `OneMonth` | 最近 1 月 |
| `OneYear` | 最近 1 年 |

**自定义日期区间**：格式 `YYYY-MM-DD..YYYY-MM-DD`，开始日期不能晚于结束日期。

示例：
- `"2025-06-01..2025-12-31"` — 2025 年下半年
- `"2026-06-01..2026-06-16"` — 本月至今

### 4.3 权威过滤 (`AuthInfoLevel`)

| 值 | 说明 |
|----|------|
| `0` | 全部结果（默认） |
| `1` | 仅返回权威来源（gov、edu、官方站点等） |

### 4.4 查询改写 (`QueryRewrite`)

- `false`（默认）：原样使用查询词
- `true`：服务端先将口语化长查询改写为精准搜索式 query，再执行搜索

**适用场景**：
- 口语化的长问题（如"最近有什么好看的人工智能电影推荐吗"）
- 首次搜索结果不理想时

### 4.5 结果摘要 (`NeedSummary`)

- 仅 `web` 类型生效
- `true`：返回每条结果的 AI 生成摘要 (`Summary` 字段)
- `false`：只返回原始片段 (`Snippet`)

---

## 5. 响应格式

### 5.1 网页搜索响应

```json
{
  "ResponseMetadata": {
    "RequestId": "...",
    "Action": "WebSearch",
    "Version": "2025-01-01"
  },
  "Result": {
    "ResultCount": 10,
    "TimeCost": 123,
    "WebResults": [
      {
        "SortId": "1",
        "Title": "论文标题或页面标题",
        "Url": "https://example.com/page",
        "SiteName": "网站名称",
        "AuthInfoDes": "权威来源标识（如有）",
        "Summary": "AI 生成的页面摘要",
        "Snippet": "原始匹配片段",
        "PublishTime": "2025-06-15"
      }
    ]
  }
}
```

### 5.2 图片搜索响应

```json
{
  "Result": {
    "ResultCount": 5,
    "TimeCost": 80,
    "ImageResults": [
      {
        "SortId": "1",
        "Title": "图片标题",
        "Image": {
          "Url": "https://example.com/image.jpg",
          "Width": 1920,
          "Height": 1080,
          "Shape": "landscape"
        }
      }
    ]
  }
}
```

### 5.3 错误响应

```json
{
  "ResponseMetadata": {
    "Error": {
      "Code": "10403",
      "Message": "invalid api key"
    }
  }
}
```

---

## 6. 错误码速查

| 错误码 | 含义 | 处理 |
|--------|------|------|
| `10400` | 参数错误 | 检查 Query、Count、TimeRange 格式 |
| `10402` | 搜索类型非法 | 仅支持 `web` / `image` |
| `10403` | API Key 无效 | 确认 Key 来源，非方舟 Key |
| `10406` | 免费额度耗尽 | 等待下月重置 或 充值 |
| `10407` | 无可用免费策略 | 检查账户状态 |
| `10408` | 后付费欠费 | 充值（24h 内可恢复） |
| `10409` | 套餐不匹配 | 更换搜索模式 |
| `10412` | 套餐额度不足 | 付费充值 |
| `10500` | 服务内部错误 | 2-3 秒后重试 |
| `429` | 频率过高 | 降频重试，建议并发 ≤ 5 |
| `700429` | 免费链路限流 | 降频重试 |
| `100013` | 子账号未授权 | 授权 `TorchlightApiFullAccess` |
| `401` | AK/SK 无效 | 检查或改用 API Key |

---

## 7. 完整调用示例

### 7.1 Python（使用 API Key）

```python
import requests
import json

API_KEY = "your_api_key_here"
URL = "https://open.feedcoopapi.com/search_api/web_search"

def web_search(query: str, count: int = 10, time_range: str = None, 
               auth_only: bool = False, rewrite: bool = False) -> dict:
    """火山引擎联网搜索"""
    body = {
        "Query": query,
        "SearchType": "web",
        "Count": min(count, 50),
        "NeedSummary": True,
    }
    
    if time_range:
        body["TimeRange"] = time_range
    
    if auth_only:
        body["Filter"] = {"AuthInfoLevel": 1}
    
    if rewrite:
        body["QueryControl"] = {"QueryRewrite": True}
    
    headers = {
        "Content-Type": "application/json",
        "X-Traffic-Tag": "skill_web_search_common",
        "Authorization": f"Bearer {API_KEY}",
    }
    
    response = requests.post(URL, headers=headers, json=body, timeout=30)
    response.raise_for_status()
    data = response.json()
    
    # 检查错误
    error = (data.get("ResponseMetadata") or {}).get("Error")
    if error:
        raise Exception(f"API Error [{error['Code']}]: {error['Message']}")
    
    return data["Result"]

# 示例用法
if __name__ == "__main__":
    # 基础搜索
    result = web_search("大模型最新进展 2025")
    print(f"找到 {result['ResultCount']} 条结果，耗时 {result['TimeCost']}ms")
    for item in result["WebResults"]:
        print(f"[{item['SortId']}] {item['Title']}")
        print(f"    {item['Url']}")
        print(f"    {item.get('Summary', '')[:200]}...")
        print()
    
    # 图片搜索
    result = web_search("故宫博物院", count=5, time_range="OneMonth")
    
    # 权威来源搜索
    result = web_search("COVID-19 疫苗有效性", auth_only=True)
    
    # 口语化查询改写
    result = web_search("最近很火的那个AI画画的工具有哪些推荐", rewrite=True)
```

### 7.2 使用 skill 脚本（本项目）

```bash
# 基础搜索
python .claude/skills/byted-web-search/scripts/web_search.py "transformer 注意力机制"

# 指定条数
python .claude/skills/byted-web-search/scripts/web_search.py "大模型" -c 20

# 最近一周
python .claude/skills/byted-web-search/scripts/web_search.py "AI news" --time-range OneWeek

# 仅权威来源
python .claude/skills/byted-web-search/scripts/web_search.py "政策" --auth-level 1

# 开启查询改写
python .claude/skills/byted-web-search/scripts/web_search.py "最近有什么好看的人工智能新电影" --query-rewrite

# 图片搜索
python .claude/skills/byted-web-search/scripts/web_search.py "故宫" -t image -c 3

# 自定义时间范围
python .claude/skills/byted-web-search/scripts/web_search.py "GPT-5" --time-range 2025-06-01..2026-06-16
```

### 7.3 cURL

```bash
# 基础搜索
curl -X POST "https://open.feedcoopapi.com/search_api/web_search" \
  -H "Content-Type: application/json" \
  -H "X-Traffic-Tag: skill_web_search_common" \
  -H "Authorization: Bearer $WEB_SEARCH_API_KEY" \
  -d '{
    "Query": "transformer attention mechanism",
    "SearchType": "web",
    "Count": 10,
    "NeedSummary": true,
    "TimeRange": "OneYear"
  }'

# 图片搜索
curl -X POST "https://open.feedcoopapi.com/search_api/web_search" \
  -H "Content-Type: application/json" \
  -H "X-Traffic-Tag: skill_web_search_common" \
  -H "Authorization: Bearer $WEB_SEARCH_API_KEY" \
  -d '{
    "Query": "故宫博物院",
    "SearchType": "image",
    "Count": 5
  }'
```

---

## 8. 搜索结果不理想时的调优策略

| 问题 | 解决方案 |
|------|---------|
| 结果不准确 | 换用简称/全称/别名重试 |
| 口语化查询召回差 | 添加 `--query-rewrite` |
| 需要最新信息 | `--time-range OneDay` |
| 需要权威来源 | `--auth-level 1` |
| 需要特定时间段 | `--time-range YYYY-MM-DD..YYYY-MM-DD` |
| 结果太少 | 去掉修饰词，只保留核心实体词；增大 `--count` |
| 需要图片 | `--type image` |
| 2-3 次重试无效 | 坦诚说明证据不足，勿编造结论 |

---

## 9. QPS 与限流

- **建议并发**：单 Key ≤ 5
- **超限响应**：HTTP 429 或错误码 `FlowLimitExceeded`
- **处理**：降低并发，指数退避重试

---

## 10. 环境变量

| 变量 | 说明 | 本项目状态 |
|------|------|-----------|
| `WEB_SEARCH_API_KEY` | 联网搜索 API Key | ✅ 已配置 |
| `VOLCENGINE_ACCESS_KEY` | AK/SK 认证 - Access Key | ❌ 未配置 |
| `VOLCENGINE_SECRET_KEY` | AK/SK 认证 - Secret Key | ❌ 未配置 |
