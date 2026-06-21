"""Phase 2: 主 Agent 的 Pydantic Schemas + Prompt 模板。

包含三大核心节点的输出 schema：
  - IntentClassifyResult: 意图分类（business/chat/meta/unsupported）
  - ScenarioPlanResult: 业务场景的执行计划（含 tools[] 数组）
  - EvaluateCompletionResult: 完成度评估

LLM 调用通过 llm_client_v2.chat_json(schema=...) 强制输出符合 schema 的 JSON。

17 个业务场景在 SCENARIOS 字典中定义，供 prompt 渲染时枚举。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
# 17 个业务场景定义（与计划文档 §1.2 一致）
# ═══════════════════════════════════════════════════════════════


SCENARIOS: dict[str, dict[str, str]] = {
    "S1": {
        "name": "文献调研 / 筛选",
        "description": "搜索某主题的论文并按相关性排序，**不下载**",
        "agent": "ingest",
        "permissions": "search",
        "example": "找些 transformer 论文",
    },
    "S2": {
        "name": "文献综述生成",
        "description": "完整 7 阶段流水线：搜索→评估→下载→转换→索引→排序→综述",
        "agent": "ingest",
        "permissions": "search + download",
        "example": "写一篇自监督学习的综述",
    },
    "S3": {
        "name": "每日前沿追踪 (订阅)",
        "description": "创建订阅；后台 Celery Beat 定时检查、新论文推送",
        "agent": "subscription",
        "permissions": "subscription + notification",
        "example": "订阅扩散模型方向",
    },
    "S4": {
        "name": "论文精读 / 提炼",
        "description": "对已入库的单篇论文做深度知识提取（方法/贡献/局限）",
        "agent": "tool",
        "permissions": "—",
        "example": "精读这篇 Attention is All You Need",
    },
    "S5": {
        "name": "方法对比",
        "description": "对比两个或多个方法/模型，搜索 + LLM 综合分析",
        "agent": "ingest",
        "permissions": "search",
        "example": "对比 ViT 和 Swin Transformer",
    },
    "S6": {
        "name": "研究空白分析",
        "description": "基于已入库语料找未被覆盖的方向",
        "agent": "clustering",
        "permissions": "—",
        "example": "这领域哪里没人做",
    },
    "S7": {
        "name": "进度查看",
        "description": "查看上次任务/项目当前状态（即时返回，无需 Celery）",
        "agent": "tool",
        "permissions": "—",
        "example": "上次那个任务怎样了",
    },
    "S8": {
        "name": "研究方向聚类 + 全景图",
        "description": "K-means + HDBSCAN 聚类，生成 t-SNE 2D 坐标",
        "agent": "clustering",
        "permissions": "—",
        "example": "把库里论文分一下方向",
    },
    "S9": {
        "name": "引用追溯",
        "description": "从种子论文按 Semantic Scholar 引用网络多层追溯",
        "agent": "citation_chase",
        "permissions": "citation_chase",
        "example": "从这篇追下去找相关工作",
    },
    "S10": {
        "name": "RAG 问答（已入库）",
        "description": "对已入库语料做带引用的学术问答",
        "agent": "rad_query",
        "permissions": "—",
        "example": "我以前看过的论文里关于 attention 加速的方法",
    },
    "S11": {
        "name": "批量搜索",
        "description": "从 csv/json 列表中批量搜索 100+ 篇",
        "agent": "ingest",
        "permissions": "search + download",
        "example": "我有个 csv 里 100 个标题都搜一下",
    },
    "S12": {
        "name": "学术翻译 / 术语库",
        "description": "中英学术术语翻译；构建项目术语库",
        "agent": "translation",
        "permissions": "—",
        "example": "翻成英文学术关键词",
    },
    "S13": {
        "name": "视频解析",
        "description": "分享链接 → yt-dlp 下载 → whisper 转写 → LLM 摘要",
        "agent": "video",
        "permissions": "video_download",
        "example": "看看这个抖音视频 https://v.douyin.com/XXX",
    },
    "S14": {
        "name": "导出 / 清理",
        "description": "导出 BibTeX/JSON；清理项目",
        "agent": "tool",
        "permissions": "clean (写) 或 —",
        "example": "导成 BibTeX",
    },
    "S15": {
        "name": "iOS 自动化",
        "description": "iOS 端工具：日历/提醒/通知/文件",
        "agent": "ios_tool",
        "permissions": "iOS 端可能再请求一次系统权限",
        "example": "加到明天日历",
    },
    "S16": {
        "name": "运维操作（开发者）",
        "description": "service / docker / apt / pip 等运维工具",
        "agent": "tool",
        "permissions": "shell_exec / package_install（强制确认）",
        "example": "服务跑没",
    },
    "S17": {
        "name": "记忆操作",
        "description": "查询用户偏好 / 主动记住某事 / 检索历史",
        "agent": "tool",
        "permissions": "—",
        "example": "你还记得我研究啥",
    },
}


SCENARIO_IDS = list(SCENARIOS.keys())


def render_scenario_list() -> str:
    """渲染 17 个业务场景为 prompt 用的 Markdown 表格。"""
    lines = ["| ID | 场景 | 描述 | 触发示例 |", "|---|---|---|---|"]
    for sid, sc in SCENARIOS.items():
        lines.append(f"| {sid} | {sc['name']} | {sc['description']} | {sc['example']} |")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Pydantic Schemas - LLM 输出强约束
# ═══════════════════════════════════════════════════════════════


class IntentClassifyResult(BaseModel):
    """节点 1: 意图分类结果。"""
    intent_kind: Literal["business", "chat", "meta", "unsupported"] = Field(
        ...,
        description="意图大类：business=匹配业务场景；chat=闲聊/问候；meta=Agent 自我认知/偏好；unsupported=能力外请求",
    )
    scenario_id: Optional[Literal[
        "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9",
        "S10", "S11", "S12", "S13", "S14", "S15", "S16", "S17",
    ]] = Field(
        None,
        description="当 intent_kind=business 时必填，指向 17 个场景之一；其他类型留 null",
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="置信度 0-1")
    reasoning: str = Field(..., max_length=200, description="简短中文推理（≤200字）")


class ToolCallSpec(BaseModel):
    """LLM 一次性返回的单个工具/子Agent 调用规约。"""
    call_id: str = Field(..., description="本轮内唯一 ID，便于追踪")
    kind: Literal["sub_agent", "tool", "ios_tool", "ask_user"] = Field(
        ...,
        description="调用类型：sub_agent=重量子Agent；tool=本地CLI工具；ios_tool=iOS端工具；ask_user=向用户提问",
    )
    name: str = Field(..., description="子 Agent type 或 tool 名称")
    arguments: dict[str, Any] = Field(default_factory=dict, description="调用参数")
    depends_on: list[str] = Field(
        default_factory=list,
        description="其他 call_id 列表；非空表示依赖；同批无依赖的并行执行",
    )


class ClarificationQuestion(BaseModel):
    """ask_user_question 单题结构。"""
    id: str = Field(..., description="题目唯一 ID")
    question: str = Field(..., description="中文问题文本")
    type: Literal["single_choice", "multi_choice", "open"] = Field(...)
    options: list[str] = Field(default_factory=list, description="选项（仅 *_choice 题型）")


class ScenarioPlanResult(BaseModel):
    """节点 2A: 业务场景执行计划。"""
    scenario_id: str = Field(..., description="对应的场景 ID（S1~S17）")
    summary: str = Field(
        ..., max_length=300,
        description="给用户看的方案摘要（中文，<=300 字）",
    )
    needs_clarification: bool = Field(
        False,
        description="是否需要先问用户澄清问题（信息不足）",
    )
    clarification_questions: list[ClarificationQuestion] = Field(
        default_factory=list,
        description="needs_clarification=true 时的问题列表",
    )
    needs_approval: bool = Field(
        False,
        description="是否需要用户先批准 plan 卡片（涉及大量下载/敏感操作时设 true）",
    )
    permissions_required: list[Literal[
        "search", "download", "citation_chase", "subscription",
        "notification", "video_download", "shell_exec", "package_install",
    ]] = Field(default_factory=list, description="本计划需要的权限")
    estimated_time_seconds: int = Field(0, description="预估总耗时（秒）")
    tools: list[ToolCallSpec] = Field(
        default_factory=list,
        description="**LLM 一次性返回**的所有工具/子Agent 调用清单",
    )


class EvaluateCompletionResult(BaseModel):
    """节点 4: 完成度评估。"""
    satisfied: bool = Field(..., description="本轮执行是否已满足用户需求")
    reasoning: str = Field(..., description="判断理由（中文）")
    needs_more_tools: list[ToolCallSpec] = Field(
        default_factory=list,
        description="satisfied=false 时的下一批工具调用",
    )
    final_message: str = Field(
        "",
        description="给用户的自然语言回复（satisfied=true 时必填）",
    )


# ═══════════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════════


INTENT_CLASSIFY_SYSTEM = """你是 Paper Agent v3 的意图分类节点。任务是判断用户的最新一条消息属于哪种意图。

## 四种意图类别

1. **business**：用户在请求 Paper Agent 的某个产品能力（见下方 17 个业务场景）
2. **chat**：闲聊、问候、寒暄、单字回应（"好的"/"谢谢"/"哈喽"等）
3. **meta**：关于 Agent 自身的元请求（"你是谁"、"你能做什么"、设置偏好"我喜欢 X"）
4. **unsupported**：明确在 Paper Agent 能力外的请求（让 Agent 写 Python 代码、做菜、聊娱乐八卦等）

## 17 个业务场景

{scenario_list}

## 输出要求

- intent_kind=business 时，scenario_id 必填，给出最匹配的场景
- 其他类型，scenario_id 留 null
- confidence: 你的判断置信度（0~1）。如果只是模糊匹配或可能有误，给 0.3~0.6
- reasoning: 中文简短解释，<=200 字
- 严格按 JSON Schema 输出，不要解释、不要 markdown 包裹

## 示例

用户："找点 transformer 论文" → intent_kind=business, scenario_id=S1
用户："你好" → intent_kind=chat, scenario_id=null
用户："你是用什么模型？" → intent_kind=meta, scenario_id=null
用户："帮我写一段 Python" → intent_kind=unsupported, scenario_id=null
"""


SCENARIO_PLAN_SYSTEM = """你是 Paper Agent v3 的场景规划节点。已知用户意图命中场景 **{scenario_id}: {scenario_name}**。

## 场景说明
{scenario_description}
- 主实现：{scenario_agent}
- 权限：{scenario_permissions}

## 你的任务

1. 判断信息是否充足。如果用户描述模糊（如"找些论文"但没给方向），设 `needs_clarification=true` 并生成 1-3 个澄清问题。
2. 如果场景涉及下载、订阅、视频下载、Shell 执行等敏感操作，设 `needs_approval=true`。让用户看 plan 卡片后确认。
3. 生成 `tools[]` 列表：**一次性**列出所有要执行的工具/子Agent 调用。
   - 独立无依赖的同批返回 → 并行执行
   - 有顺序依赖的设 `depends_on=[其他call_id]`
4. `summary` 给用户看的方案描述（中文，≤300 字）

## 工具与子 Agent 选择规则

- **重量任务（多阶段流水线）** → 用 `kind=sub_agent`：
  - 文献调研/综述 → `name="ingest"`
  - 引用追溯 → `name="citation_chase"`
  - 聚类/全景图 → `name="clustering"`
  - RAG 问答 → `name="rad_query"`
  - 视频解析 → `name="video"`
  - 翻译/术语库 → `name="translation"`
- **即时查询** → 用 `kind=tool`：
  - `paper_status`、`get_paper_abstract`、`list_sources`、`read_paper`
  - `search_memory`、`search_knowledge`、`search_library`
  - `get_user_preference`、`extract_to_long_term`
- **iOS 端** → `kind=ios_tool`，name 以 `ios_` 开头
- **询问用户** → `kind=ask_user`，name=`ask_user_question`

## 输出

严格按 JSON Schema 输出，不要解释、不要 markdown 包裹。
"""


EVALUATE_COMPLETION_SYSTEM = """你是 Paper Agent v3 的完成度评估节点。

刚才一批 `tools[]` 已经执行完毕，你看到了它们的结果。你的任务是判断：
1. 用户的需求是否**已经满足**？
2. 如果没满足，下一步需要再调用哪些工具？

## 判断准则

- 用户问"找论文" 而最终找到 0 篇 → **未满足**，可能要换关键词
- 用户问"对比 A 和 B" 而只搜了 A → **未满足**，需要再搜 B
- 用户问"综述" 而综述文件已生成 → **满足**
- 用户问"读这篇论文" 而 read_paper 返回了内容 → **满足**

## 输出要求

- `satisfied=true` 时，`final_message` 必填，给用户写一段自然语言总结（中文）
- `satisfied=false` 时，`needs_more_tools` 至少 1 个调用

为避免无限循环，整轮最多 3 次 evaluate-execute 迭代。

严格按 JSON Schema 输出。
"""


def build_intent_classify_prompt() -> str:
    """渲染 intent_classify 完整 system prompt（含 17 场景列表）。"""
    return INTENT_CLASSIFY_SYSTEM.format(scenario_list=render_scenario_list())


def build_scenario_plan_prompt(scenario_id: str) -> str:
    """渲染特定场景的 scenario_plan system prompt。"""
    sc = SCENARIOS.get(scenario_id)
    if not sc:
        raise ValueError(f"Unknown scenario_id: {scenario_id}")
    return SCENARIO_PLAN_SYSTEM.format(
        scenario_id=scenario_id,
        scenario_name=sc["name"],
        scenario_description=sc["description"],
        scenario_agent=sc["agent"],
        scenario_permissions=sc["permissions"],
    )


INLINE_REPLY_SYSTEM = """你是 Paper Agent v3 的对话节点 — 处理非业务请求（闲聊 / 元请求 / 能力外请求）。

## 行为准则

- **chat（闲聊）**：友好简短地回应。
- **meta（元请求）**：
  - 介绍自己：你是 Paper Agent v3 — 个人 AI 科研助理，能帮用户搜索/下载/阅读/综述论文、解析视频、追踪前沿等。
  - 偏好设置：可主动调用 `extract_to_long_term` 工具记住用户偏好。
- **unsupported（能力外）**：礼貌说明该请求不在 Paper Agent 的科研助理能力范围内，并推荐 3 个实际能力（例如"我擅长找论文、综述、视频学习总结，需要试试吗？"）。

## 工具使用

只允许调用以下轻量工具，禁用 launch_sub_agent / propose_plan：
- `search_memory`：查历史对话
- `get_user_preference`：查用户偏好
- `extract_to_long_term`：主动记忆
- `paper_status`、`list_sources` 等纯查询工具

回复采用流式 thinking + 最终 text。"""


__all__ = [
    "SCENARIOS",
    "SCENARIO_IDS",
    "render_scenario_list",
    "IntentClassifyResult",
    "ScenarioPlanResult",
    "EvaluateCompletionResult",
    "ToolCallSpec",
    "ClarificationQuestion",
    "build_intent_classify_prompt",
    "build_scenario_plan_prompt",
    "INTENT_CLASSIFY_SYSTEM",
    "SCENARIO_PLAN_SYSTEM",
    "EVALUATE_COMPLETION_SYSTEM",
    "INLINE_REPLY_SYSTEM",
]
