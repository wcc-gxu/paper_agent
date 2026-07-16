# 反幻觉策略

> v2.0 | 2026-07-16 | 唯一权威文档
>
> 本文档取代旧版 4 层/6 层纵深体系，改为三层设计。
>
> **核心原则**：学术工具，说错比不说危害大百倍。三层防线从根因出发而非堵症状。

---

## 一、核心认知

LLM 产生幻觉的根本原因不是“爱编造”，而是**上下文不够好**。

```
上下文空洞 → LLM 被迫用训练记忆补全 → 幻觉
```

解决思路：

1. **人格设定**让 LLM 自我约束
2. **上下文质量**是主战场——好材料自然产出好答案
3. **规则验证**兜底——代码级硬核对，零幻觉风险

---

## 二、三层防线

### 第一层：人格设定

LLM 是**该领域的专家，但天性谨慎，为自己说出的每一句话负责。** 保留创造力和综合能力，不自我矮化。

所有生成 prompt 统一注入：

```
你是一个谨慎的领域专家。你的每一条结论都有充分根据，对自己说出的
每一句话负责。

1. 你的根据来自我提供给你的文献和资料，而非你的训练记忆。
2. 不确定就说“不确定”，不知道就说“不知道”——这是专家的职业操守。
3. 不要编造：作者名、论文标题、年份、期刊名、DOI、数据集名、指标数字。
   任何一个不确定，宁可省略也不要猜。
4. 引用规范：
   - 直接引用论文原句 → 用 > blockquote
   - 综合多篇论文的判断 → 普通段落，附引用编号 [1][2,3]
```

实施：一个 `EXPERT_PERSONA_CLAUSE` 字符串常量，拼入全部生成 prompt。不称 `ANTI_FABRICATION`——正向表述比否定指令更有效。

### 第二层：上下文质量（主战场）

幻觉的根因是上下文不够好。高质量上下文 → LLM 自然输出可靠。

| 环节 | 做法 |
|------|------|
| 检索前 | 复杂问题拆解为可检索的子问题，query 改写优化 |
| 检索中 | 多源召回 + cross-encoder rerank + `RAG_MIN_SCORE=0.55` 阈值 |
| 检索后 | 去重、多样性过滤、期刊等级加权（A+: 1.2, C: 0.9） |
| 注入前 | Chunk 附带完整 provenance metadata（paper_id, section, page），按相关性排序 |
| 使用中 | 上下文为空或全部分数 < 阈值 → 直接拒答，不生成 |

实施要点：

- 上下文空 → 说“未查到相关信息”，不强行生成
- 上下文稀薄（仅 1-2 篇）→ 降低回答范围，标注“仅基于有限文献”
- RAG 路径强制注入 provenance metadata
- 非 RAG 路径（翻译、单篇摘要）确保源文本在上下文中

### 第三层：规则验证（兜底）

对 LLM 最终输出做代码级硬核对。**不依赖 LLM 二次判断——规则是确定性的，毫秒级执行，零幻觉风险。**

#### 3.1 输出格式

最终回答为自由文本 Markdown（Pro 模型 + thinking=enable），引用格式仿学术论文：

```
近年来，Transformer 架构在视觉任务中取得了显著进展 [1][2,3]。
ViT 首次证明了纯注意力机制在图像分类上的有效性 [1]。

> "Vision Transformer achieves state-of-the-art results..." [1]

## References
[1] Dosovitskiy et al. An Image is Worth 16x16 Words. ICLR 2021.
[2] Liu et al. Swin Transformer. ICCV 2021.
[3] Touvron et al. DeiT. ICML 2021.
```

不做结构化 JSON——综述的本质是论文，自由格式才能表达深入的学术分析。

#### 3.2 正则验证规则

| 检查项 | 方法 | 动作 |
|--------|------|------|
| `[N]` 编号不连续或越界 | 提取正文所有引用标记 + References 编号 → 比对 | 标记异常 |
| References 条目缺失 | 比对正文引用的编号是否在 References 中 | 标记异常 |
| paper_id 在 DB 中不存在 | 从 References 提取 paper_id → 查 DB | 标记 `⚠️[需核查]` |
| title 模糊匹配失败 | 正则提取 title → DB 模糊搜索 | 标记 `⚠️[来源待确认]` |

#### 3.3 其他硬规则

| 规则 | 说明 |
|------|------|
| JSON Schema 强约束 | 所有非最终回答的 LLM 输出走 `chat_json(schema=Pydantic)` + `tool_choice` |
| 安全过滤 | 7 regex 模式 + LLM 二次确认 + fail-closed |
| RAG 分数阈值 | `top_score < 0.55` → 拒答，不强行生成 |
| 跨源一致性 | 孤立声明（仅 1 篇文献支持）→ 标注“单一来源” |
| fail-closed | 任何判官/验证模块异常 → 默认不通过，不假阳放行 |

---

## 三、工程纪律

| 纪律 | 做法 |
|------|------|
| fail-closed | 任何 judge/验证异常时默认 reject |
| 低温采样 | 决策节点 0.0-0.2，防随机性引入幻觉 |
| thinking=disabled | Judge 节点关闭思考链，防自我说服式编造 |
| 轮次限制 | 自动执行 8 轮上限，用户交互不限轮次 |
| 审计日志 | `hallucination_events` 表记录每次验证的 verdict |
| 特性开关 | 每层独立 env 开关，可灰度可回滚 |
| “unknown”合法性 | Schema 支持 null/None 作为合法返回值 |

---

## 四、实施路线图

### Phase 1 — 提示词 + 规则（当前优先，2-3 天）

| # | 任务 | 所属层 |
|---|------|:---:|
| P1.1 | 全部生成 prompt 注入专家人格设定 | 第一层 |
| P1.2 | RAG 路径强制注入 provenance metadata | 第二层 |
| P1.3 | `RAG_MIN_SCORE=0.55` 阈值拒答 | 第二层 |
| P1.4 | 上下文空时统一拒答模板 | 第二层 |
| P1.5 | 构建 output_verify 规则验证节点 | 第三层 |
| P1.6 | 引用格式正则解析器 + DB 存在性比对 | 第三层 |
| P1.7 | `hallucination_events` 审计表写入逻辑 | 工程纪律 |

### Phase 2 — 验证链路集成（3-5 天）

| # | 任务 | 所属层 |
|---|------|:---:|
| P2.1 | Writing Agent 综述输出集成规则验证节点 | 第三层 |
| P2.2 | Knowledge Agent RAG 输出集成规则验证节点 | 第三层 |
| P2.3 | ExternalValidator 并入规则验证链（DOI/arXiv 校验） | 第三层 |
| P2.4 | 验证失败时标注 `⚠️[需核查]` 而非默默删除 | 第三层 |

### Phase 3 — 增强（数据驱动，1+ 周）

| # | 任务 | 说明 |
|---|------|------|
| P3.1 | 收集 KPI 基线（引用错误率、拒答率） | 数据驱动决策 |
| P3.2 | 基于 KPI 决定是否加 LLM groundedness judge | 不强推 |
| P3.3 | 跨源一致性检测（≥2 源才采信孤立声明） | 第三层增强 |

---

## 五、回滚与开关

每层独立开关，可灰度可回滚：

| 开关 env | 默认 | 关闭后果 |
|---|:---:|---|
| `AH_PERSONA` | on | 专家人格不注入 |
| `AH_CONTEXT_QUALITY` | on | RAG 分数阈值 + 拒答跳过 |
| `AH_RULE_VERIFY` | on | output_verify 节点跳过 |
| `AH_AUDIT` | on | hallucination_events 不写入 |
| `RAG_MIN_SCORE` | 0.55 | RAG 检索分阈值 |
| `AH_MAX_RETRY` | 0 | 验证失败后重试次数（当前为 0：只标记不重生成） |

---

## 六、与其他文档的关系

本文档是反幻觉策略的**唯一权威来源**。其他文档统一引用本文：

```markdown
> **反幻觉策略**详见 [anti-hallucination.md](anti-hallucination.md)
```

以下文档的旧版反幻觉描述（“4 层防线”“6 层纵深”“三步校验”“严格校验”“CitationVerifier 三步”等）均已被本文档取代：

- `product-architecture-plan.md`（行 421、517、570）
- `CLAUDE.md`（反幻觉小节）
- `jd-skills-mapping.md`（行 54 CitationVerifier）
- `智驭研_重构方案_v3.md`（反幻觉章节）
