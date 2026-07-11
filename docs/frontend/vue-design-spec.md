# 智驭·研 Vue 前端 — 设计/交互文档

> 对应产品文档 [vue-product-spec](./vue-product-spec.md)
>
> 技术栈：Vue 3 + TypeScript + Vite + Naive UI + Pinia + KaTeX + ECharts
>
> 日期：2026-07-11

---

## 目录

1. [设计原则与规范](#1-设计原则与规范)
2. [全局布局](#2-全局布局)
3. [响应式策略](#3-响应式策略)
4. [主题与样式变量](#4-主题与样式变量)
5. [Chat 交互设计](#5-chat-交互设计)
6. [消息气泡组件详细规范](#6-消息气泡组件详细规范)
7. [状态管理与数据流](#7-状态管理与数据流)
8. [WebSocket 重连与离线处理](#8-websocket-重连与离线处理)
9. [错误与空状态](#9-错误与空状态)
10. [动画与过渡](#10-动画与过渡)
11. [无障碍访问](#11-无障碍访问)
12. [组件 Props 协议](#12-组件-props-协议)

---

## 1. 设计原则与规范

### 1.1 核心设计原则

| 原则 | 说明 |
|------|------|
| **移动优先** | 基础布局按 375px 设计，逐步增强至桌面 |
| **渐进式披露** | 默认折叠次要信息，用户按需展开 |
| **状态可见** | 每次操作有明确反馈（loading/spinner/百分比/完成标记） |
| **容错优先** | Markdown 渲染失败降级为纯文本、LaTeX 失败显示原始公式、WS 断开自动重连 |

### 1.2 设计参考

- ChatGPT 网页版（侧边栏 + 对话流）
- GLM 智谱清言（中文排版、卡片交互）
- Linear（进度卡片设计）

### 1.3 视觉度量

| 属性 | 桌面 | 移动 |
|------|------|------|
| 侧边栏宽度 | 260px | 全屏抽屉 |
| 主内容最大宽 | 800px | 100vw |
| 消息气泡最大宽 | 85% | 90% |
| 圆角 | 12px（卡片）/ 8px（按钮） | 同 |
| 间距单位 | 4px 基准（4/8/12/16/24/32） | 同 |

### 1.4 颜色系统

| 语义色 | 色值 | 用途 |
|--------|------|------|
| 主色 | `#4C6EF5` (蓝) | 按钮、链接、选中态 |
| 成功 | `#12B886` (绿) | 引用 ✓、任务完成 ✅ |
| 警告 | `#FAB005` (黄) | 处理中 🔄、低置信度 |
| 错误 | `#F03E3E` (红) | 引用 ❌、错误、失败 |
| 中性 | `#F8F9FA` (灰白) | 卡片背景、空状态 |
| 文本主 | `#212529` | 正文 |
| 文本次 | `#868E96` | 时间戳、状态提示 |

---

## 2. 全局布局

### 2.1 AppLayout 组件

```vue
<template>
  <div class="app-layout">
    <!-- 桌面端：固定侧边栏 -->
    <Sidebar
      v-if="!isMobile || drawerOpen"
      :collapsed="sidebarCollapsed"
      @close="drawerOpen = false"
    />

    <!-- 主内容 -->
    <main class="main-content">
      <router-view />
    </main>

    <!-- 移动端：底部 Tab -->
    <MobileNav v-if="isMobile" />
  </div>
</template>
```

### 2.2 Sidebar 组件规范

```
┌─ Sidebar ────────────────────────┐
│ [Logo] 智驭·研           [☰ 折叠]│  ← 桌面端可折叠
│ ─────────────────────────────────│
│ [+ 新会话]        按钮 (fullwidth)│
│ ─────────────────────────────────│
│ 会话项                             │
│ ┌──────────────────────────────┐ │
│ │ 📝 自动驾驶对抗攻击           │ │
│ │    2026-07-10 14:30    [···] │ │  ← 右键：重命名/删除
│ └──────────────────────────────┘ │
│ ┌──────────────────────────────┐ │
│ │ 📝 transformer 综述           │ │
│ │    2026-07-09 10:15           │ │
│ └──────────────────────────────┘ │
│ ─────────────────────────────────│
│ 导航链接                           │
│ 📄 论文管理                        │
│ 📚 知识库                          │
│ 📖 术语词表                        │
│ 📋 任务管理                        │
│ ✏️ 写作编辑                        │
│ ─────────────────────────────────│
│ ⚙ 设置                            │
│ 👤 user_display_name    [退出]    │
└──────────────────────────────────┘
```

**交互行为**：
- 点击折叠按钮 → Sidebar 缩小至图标模式（仅显示 icon，hover 展开 tooltip）
- 右侧 3px 拖拽边框 → 可调整宽度 (200px - 400px)
- 当前激活的会话/页面 → 背景高亮 `rgba(76, 110, 245, 0.08)`
- 会话列表按最后活跃时间倒序

### 2.3 MobileNav 组件规范

```
┌─────────────────────┐
│      主内容区         │
├─────────────────────┤
│ 💬       📚       ⚙  │  3 个核心 Tab（其余进 "更多"）
│ 聊天      知识库    设置 │
└─────────────────────┘
```

- 固定底部 56px 高
- 激活 Tab：主色填充
- 非激活 Tab：灰色

---

## 3. 响应式策略

### 3.1 断点定义

| 断点 | 最小宽 | 描述 | 布局 |
|------|--------|------|------|
| **xs** | 0 | 手机竖屏 | 单列，底部 Tab |
| **sm** | 640px | 手机横屏 / 小平板 | 单列，底部 Tab |
| **md** | 768px | 平板 | Sidebar 可折叠 |
| **lg** | 1024px | 桌面 | Sidebar 默认展开 |
| **xl** | 1280px | 大屏 | 论文卡片 4 列 |

### 3.2 各页面响应式行为

| 页面 | xs (<640) | md (768+) | lg (1024+) |
|------|-----------|-----------|------------|
| Chat | 全宽，Sidebar 抽屉 | Sidebar 可展开 | Sidebar 固定 |
| Papers | 卡片 1 列 | 卡片 2 列 | 卡片 3 列 |
| Knowledge | 树形只显示，点击弹出详情 | 树形只显示 | 左树右详情 |
| Glossary | 表格横向滚动 | 表格正常 | 表格正常 |
| Writing | 全宽编辑器 | 居中 800px | 居中 800px |
| Settings | 全宽 | 居中 600px | 居中 600px |

### 3.3 移动端特殊交互

- 左滑 → 唤出 Sidebar 抽屉（`v-touch` 或 hammer.js）
- 论文卡片 → 点击展开/折叠摘要
- 表格 → 横向滚动容器
- 返回手势 → 浏览器默认行为（`history.back()`）

---

## 4. 主题与样式变量

### 4.1 CSS 变量

```css
:root {
  /* 颜色 */
  --color-primary: #4C6EF5;
  --color-primary-hover: #3B5DE7;
  --color-success: #12B886;
  --color-warning: #FAB005;
  --color-error: #F03E3E;
  --color-bg: #FFFFFF;
  --color-bg-secondary: #F8F9FA;
  --color-text: #212529;
  --color-text-secondary: #868E96;
  --color-border: #DEE2E6;

  /* 圆角 */
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;

  /* 阴影 */
  --shadow-sm: 0 1px 3px rgba(0,0,0,.08);
  --shadow-md: 0 4px 12px rgba(0,0,0,.1);
  --shadow-lg: 0 8px 24px rgba(0,0,0,.12);

  /* 间距 */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 12px;
  --space-lg: 16px;
  --space-xl: 24px;
  --space-2xl: 32px;

  /* 字体 */
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: "SF Mono", "Fira Code", "Fira Mono", Menlo, Consolas, monospace;
  --font-size-xs: 12px;
  --font-size-sm: 14px;
  --font-size-md: 16px;
  --font-size-lg: 18px;
  --font-size-xl: 24px;
  --font-size-2xl: 32px;
}

/* 深色主题 */
[data-theme="dark"] {
  --color-bg: #1A1B1E;
  --color-bg-secondary: #25262B;
  --color-text: #C1C2C5;
  --color-text-secondary: #909296;
  --color-border: #373A40;
}
```

### 4.2 主题切换

- 初始值：`window.matchMedia('(prefers-color-scheme: dark)')`
- localStorage 用户偏好覆盖系统偏好
- Naive UI 的 `darkTheme` 配合切换
- 切换无闪烁：`<html data-theme>` 在 `<script>` 中提前设置

---

## 5. Chat 交互设计

### 5.1 消息流时序

```
用户操作                        前端渲染                        WS 消息
────────                        ────────                        ────────
点击发送
    │
    ├─────────────────────── 追加 UserBubble ──────────────────────→ 发送 message
    │                                                                     │
    │                                                                     ▼
    │                       追加 StatusBubble                  ←── status(id="s1")
    │                       "收到，正在分析..."
    │                                                                     │
    │                       追加 AskCard                     ←── ask(kind=plan)
    │                       用户批准
    │   发送 ask_reply ──────────────────────────────────────────→
    │                                                                     │
    │                       追加 PlanTodoList (N 步)
    │                       追加 ToolCard(t1)                   ←── tool/start(t1)
    │                       替换 ToolCard(t1) 进度               ←── tool/progress(t1)
    │                       替换 ToolCard(t1) ✓完成              ←── tool/result(t1)
    │                       追加 ToolCard(t2) 进度               ←── tool/start(t2)
    │                       替换 ToolCard(t2) ✓完成              ←── tool/result(t2)
    │                       PlanTodoList 更新 step 状态
    │                                                                     │
    │                       追加 ReplyBubble                    ←── message/reply
    │                       (Markdown 渲染)
```

### 5.2 AskCard 5 种形态详细规范

#### kind=confirm

```
┌─ AskCard ──────────────────────────────┐
│ 🤖 System                              │
│                                         │
│ 确认执行文献调研？预计耗时约 15 分钟。      │
│                                         │
│         [ 确认 ]      [ 取消 ]          │
└─────────────────────────────────────────┘
```

- `input.question`: 文本标题
- `input.options`: `[{label: "确认", value: true}, {label: "取消", value: false}]`
- 两按钮水平排列，**确认**在主色，**取消**在灰色

#### kind=choice

```
┌─ AskCard ──────────────────────────────┐
│ 🤖 System                              │
│                                         │
│ 请选择调研范围：                          │
│                                         │
│  ○ 仅 arXiv 论文                         │
│  ○ arXiv + 会议论文（CVPR/ICCV/ECCV）     │
│  ○ 全部来源（含预印本）                    │
│                                         │
│              [ 确定 ]                   │
└─────────────────────────────────────────┘
```

- Radio 按钮列表
- 默认选中第一项
- 需要点**确定**才发送（避免误触）

#### kind=multi_choice

```
┌─ AskCard ──────────────────────────────┐
│ 🤖 System                              │
│                                         │
│ 请选择需要入库的论文（已选 5/10）：          │
│                                         │
│  ☑ Adversarial Attacks on AD (CVPR)     │
│  ☐ Robust Detection Methods (ICCV)      │
│  ☑ Physical Attack Survey (ECCV)        │
│  ☐ ...                                 │
│                                         │
│     [ 全部选择 ]     [ 确认入库 ]         │
└─────────────────────────────────────────┘
```

- Checkbox 列表
- **全部选择**快捷按钮
- 展示已选/总数

#### kind=text

```
┌─ AskCard ──────────────────────────────┐
│ 🤖 System                              │
│                                         │
│ 请输入补充关键词（用逗号分隔）：             │
│                                         │
│ ┌─────────────────────────────────────┐ │
│ │ adversarial, robustness, detection   │ │
│ └─────────────────────────────────────┘ │
│                                         │
│              [ 确定 ]                   │
└─────────────────────────────────────────┘
```

- 单行或多行文本输入
- `input.multiline: true` → textarea

#### kind=plan（方案审批，最高频最重要）

```
┌─ AskCard ──────────────────────────────────────┐
│ 📋 方案概览                         SmartPlan  │
│                                                 │
│ 我将按以下步骤完成任务：                            │
│                                                 │
│ ┌─ 步骤1: 文献搜索 ───────────────────────────┐ │
│ │ 🔍 搜索 arXiv + IEEE + ACM                  │ │
│ │    预计耗时: 3 分钟                           │ │
│ │    使用工具: literature_search               │ │
│ └────────────────────────────────────────────┘ │
│ ┌─ 步骤2: 论文筛选 ───────────────────────────┐ │
│ │ 📊 按引用数降序取 50 篇                       │ │
│ │    预计耗时: 1 分钟                           │ │
│ └────────────────────────────────────────────┘ │
│ ┌─ 步骤3: 下载入库 ───────────────────────────┐ │
│ │ 📥 下载 PDF + 转 Markdown                     │ │
│ │    预计耗时: 8 分钟                           │ │
│ │    使用工具: download_pdf, pdf_to_md          │ │
│ └────────────────────────────────────────────┘ │
│ ┌─ 步骤4: 综述生成 ───────────────────────────┐ │
│ │ ✏️ 生成 5 页中文综述                          │ │
│ │    预计耗时: 5 分钟                           │ │
│ └────────────────────────────────────────────┘ │
│                                                 │
│ 预估总时间: ~17 分钟                             │
│                                                 │
│ ┌────────────────────────────────────────────┐ │
│ │ 执行权限: ○ 询问确认  ● 自动执行            │ │
│ │ 异常处理: ○ 中断确认  ● 自动跳过            │ │
│ └────────────────────────────────────────────┘ │
│                                                 │
│     [ 修改计划 ]              [ 批准执行 ]       │
└─────────────────────────────────────────────────┘
```

- 步骤列表有层次缩进
- 权限/异常处理 → 开关组件
- **修改计划** → 弹出文本输入框让用户编辑 plan，发 `ask_reply` 带修改后的 plan
- **批准执行** → 后端开始调度

### 5.3 PlanTodoList 组件规范

```
┌─ Plan ──────────────────────────────────────────┐
│ ✅ Phase 1: 文献搜索              (2/2 已完成)   │
│    ✅ literature_search    50 篇                 │
│    ✅ citation_chase       +30 篇                │
│                                                  │
│ 🔄 Phase 2: 论文下载入库          (1/3 进行中)   │
│    🔄 download_pdf         28/50                 │
│    ⏳ pdf_to_md            —                     │
│    ⏳ knowledge_ingest     —                     │
│                                                  │
│ ⏳ Phase 3: 综述生成                              │
│ ⏳ Phase 4: 术语收集（后台）                       │
│ ⏳ Phase 5: AI 味校验                             │
└──────────────────────────────────────────────────┘
```

**数据流**：
- Plan 审批确认后，后端推送 → 前端渲染 PlanTodoList
- 每层条目有唯一 `step_id` / `sub_step_id`
- `tool/start` 携带 `step_id` → 锁定对应条目为 🔄
- `tool/progress` → 更新进度百分数
- `tool/result` { done } → ✅
- `tool/result` { failed } → ❌ + 错误信息

### 5.4 ToolCard 组件规范

```
// 并行工具 → 每个 ToolCard 独立
┌─ ToolCard ───────────────────────────────────────┐
│ 📊 literature_search                     (28/50) │
│ ████████████░░░░░░░░ 56%                         │
│ 正在搜索 arXiv...                                 │
└─ ToolCard ───────────────────────────────────────┘

// 完成状态
┌─ ToolCard ───────────────────────────────────────┐
│ ✅ literature_search                     (50/50) │
│ ████████████████████ 100%                        │
│ 共搜索到 50 篇论文                                │
└─ ToolCard ───────────────────────────────────────┘

// 失败状态
┌─ ToolCard ───────────────────────────────────────┐
│ ❌ download_pdf                          (0/50)  │
│ 连接超时，已重试 3 次                               │
│ [重试]                                           │
└─ ToolCard ───────────────────────────────────────┘
```

- ToolCard 与 PlanTodoList 独立渲染
- 同一个 `tool_call_id` 的 `start/progress/result` 更新同一张卡片（替换）
- 卡片底部信息随 `progress` 的 `message` 字段动态更新

---

## 6. 消息气泡组件详细规范

### 6.1 UserBubble

```
右对齐，背景 var(--color-primary)，白字
┌─── UserBubble ───────────────────────────────────┐
│ 帮我调研自动驾驶对抗攻击领域，50篇                      │
│                                                   │
│ ┌─────────┐ ┌────────────────┐                   │
│ │ 📄 arxiv │ │ 🎵 lecture.mp3│                   │
│ │ 2507a.pdf│ │ 15MB          │                   │
│ └─────────┘ └────────────────┘                   │
│                                   14:32           │
└─── UserBubble ───────────────────────────────────┘
```

- 附件卡片：图片 → 缩略图，PDF → 文件卡片，音频 → 文件卡片
- 时间戳右下角小字

### 6.2 ReplyBubble

```
左对齐，背景 var(--color-bg-secondary)
┌─── ReplyBubble ────────────────────────────────┐
│                                                 │
│ ## 自动驾驶对抗攻击综述                           │
│                                                 │
│ Adversarial attacks pose significant threats    │
│ to autonomous driving systems, particularly     │
│ in perception modules [local:pap-001] ✓ and      │
│ planning modules [local:pap-002] ✓.              │
│                                                 │
│ ### 1. 感知层的攻击方法                           │
│                                                 │
│ Eykholt et al. (2018) demonstrated that ...     │
│                                                 │
│ $$\min_{\delta} \|\delta\|_p \quad s.t. \quad    │
│ f(x+\delta) \neq y$$                             │
│                                                 │
│                                  📅 14:33        │
└─── ReplyBubble ──────────────────────────────────┘
```

- Markdown 渲染（marked + 自定义 renderer）
- 引用标记渲染为 CitationBadge（见 §6.5）
- LaTeX 公式 → KaTeX
- 长文本默认折叠（超过 2000 字显示"展开全文"）
- 底部显示 model 名 + 时间戳

### 6.3 StatusBubble

```
居中对齐，灰色小字，背景无
───────── StatusBubble ─────────
  收到，正在分析...
────────────────────────────────
```

- **重要行为**：新的 status 消息会**替换**上一条 status 消息（使用相同的 status_id）
- 如果 status_id 不同，追加新一行

### 6.4 ErrorBubble

```
左对齐，红色边框
┌─── ErrorBubble ─────────────────────────────────┐
│ ❌ 文献搜索失败：arXiv API 限流，请稍后重试。          │
│                                                  │
│                                        [重试]    │
└──────────────────────────────────────────────────┘
```

### 6.5 CitationBadge

```
// [local:pap-001] ✓ → 绿色 badge
// [ext:10.1145/xxx] ❌ → 红色 badge
// [Agent 综合] → 灰色 badge
```

```css
.citation-badge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  font-family: var(--font-mono);
  text-decoration: none;
  cursor: default;           /* 当前不可点击 */
}
.citation-badge.local    { background: #d3f9d8; color: #2b8a3e; }
.citation-badge.external { background: #ffe3e3; color: #c92a2a; }
.citation-badge.synthetic{ background: #e9ecef; color: #495057; }
```

渲染规则：
- `regular` path, `segments[]` 每个 {badge_key: [span_start, span_end]} 映射到文本位置 → 插入 Badge
- 文本中不再保留原始标记（如 `[local:pap-001]`），只保留 Badge

**渲染示意**：

```
原文：system demonstrated in [local:pap-001] ✓...
渲染：system demonstrated in [pap-001 ✓]...
                                ↑ colored inline badge
```

---

## 7. 状态管理与数据流

### 7.1 Pinia Store 设计

```
┌─ authStore ──────────────────────────────┐
│ user, accessToken, refreshToken          │
│ actions: login, logout, refreshToken     │
│ getters: isLoggedIn, isAdmin, token      │
└──────────────────────────────────────────┘
         │
         ▼
┌─ sessionStore ───────────────────────────┐
│ sessions[], activeSessionId              │
│ actions: fetchSessions, switchSession    │
│       , createSession, deleteSession     │
└──────────────────────────────────────────┘
         │
         ▼
┌─ chatStore ──────────────────────────────┐
│ messages[], isStreaming, planSteps[]     │
│ toolCards{} (keyed by tool_call_id)      │
│ actions: sendMessage, appendWsMessage    │
│       , updateToolCard, clearChat        │
└──────────────────────────────────────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌─ paper ─┐ ┌─ glossary ─┐
│ papers[]│ │ terms[]    │
└─────────┘ └────────────┘
```

### 7.2 chatStore 核心逻辑

```typescript
// 核心：处理任意 WS message 并更新状态
function handleWsMessage(msg: WsMessage) {
  switch (msg.type) {
    case 'status':
      upsertStatus(msg.payload);
      break;
    case 'message/reply':
      appendReply(msg.payload);
      break;
    case 'tool/start':
      upsertToolCard(msg.payload.tool_call_id, {
        status: 'running',
        name: msg.payload.tool_name,
        message: msg.payload.message,
        step: 0,
        total: msg.payload.total || 0,
      });
      // 同时更新 PlanTodoList 对应步骤
      if (msg.payload.step_id) {
        updatePlanStep(msg.payload.step_id, 'running');
      }
      break;
    case 'tool/progress':
      updateToolCard(msg.payload.tool_call_id, {
        step: msg.payload.step,
        total: msg.payload.total,
        message: msg.payload.message || '',
      });
      break;
    case 'tool/result':
      updateToolCard(msg.payload.tool_call_id, {
        status: msg.payload.status === 'done' ? 'done' : 'failed',
        message: msg.payload.summary || '',
        data: msg.payload.data,
      });
      if (msg.payload.step_id) {
        updatePlanStep(msg.payload.step_id, msg.payload.status);
      }
      break;
    case 'ask':
      appendAskCard(msg.payload);
      break;
    case 'error':
      appendError(msg.payload);
      break;
  }
}
```

### 7.3 消息列表顺序保证

所有消息按接收时间排序。索引使用组合键 `${timestamp}-${type}-${tool_call_id||id||''}` 保证唯一性，消息列表始终有序。

---

## 8. WebSocket 重连与离线处理

### 8.1 重连策略

```
断开
  │
  ├── 1s 后尝试重连
  ├── 2s
  ├── 4s
  ├── 8s
  ├── 16s
  ├── 30s (最大间隔)
  │
  ├── 重连成功 → 发送 sync { last_msg_id }
  │   └── 后端回放离线消息 + sync_complete
  │
  └── 连续失败 > 10 次 → 显示"连接中断"弹窗 + 手动重试按钮
```

### 8.2 离线状态 UI

```
┌─ 离线横幅 ───────────────────────────────────────┐
│ ⚠️ 连接中断，正在尝试重连... (第 3 次)  [手动重连] │
└──────────────────────────────────────────────────┘
```

- 显示在 Chat 顶部
- 所有输入框禁用
- 重连成功后自动消失
- 消息列表可正常滚动查看历史

---

## 9. 错误与空状态

### 9.1 错误状态

| 场景 | UI |
|------|----|
| 登录失败 | Toast: "用户名或密码错误" |
| Token 过期 | 自动刷新 → 失败则跳转 /login |
| API 500 | Toast: "服务器异常，请稍后重试" |
| 文件上传失败 | 输入框内文件标签变红 + 点击重传 |
| WS 工具执行失败 | ErrorBubble + [重试] 按钮 |

### 9.2 空状态

| 场景 | UI |
|------|----|
| 无会话 | ☁️ "暂无会话，点击 [+ 新会话] 开始" |
| 无论文 | 📄 "知识库为空，在对话中完成文献调研即可自动入库" |
| 无术语 | 📖 "暂无术语，完成文献调研后自动提取" |
| 无任务 | 📋 "暂无任务记录" |
| 搜索结果为空 | "未找到论文，请尝试调整搜索关键词" |

---

## 10. 动画与过渡

| 场景 | 动画 | 时长 |
|------|------|------|
| Sidebar 折叠/展开 | `width` transition | 200ms ease |
| Sidebar 抽屉（移动端） | `transform: translateX` | 250ms ease-out |
| 消息气泡追加 | `opacity 0→1` + `translateY(8px→0)` | 150ms |
| ToolCard 进度条 | `width` transition | 200ms linear |
| AskCard 出现 | `opacity 0→1` + `scale(0.98→1)` | 200ms |
| 欢迎横幅消失 | `opacity 1→0` + `max-height 300px→0` | 300ms |
| 加载/思考 | TypingDots：三点缩放闪烁 | 循环 |

---

## 11. 无障碍访问

| 要求 | 实现 |
|------|------|
| 语义化 HTML | `<header>` / `<nav>` / `<main>` / `<article>` 正确使用 |
| 键盘导航 | Tab 焦点的可见轮廓、Enter 触发按钮、Escape 关闭弹窗 |
| ARIA 标签 | `aria-label` 给无文本按钮、`aria-live="polite"` 给动态消息区域 |
| 颜色对比度 | 正文与背景 ≥ 4.5:1（WCAG AA） |
| 屏幕阅读器 | 消息通知使用 `role="alert"`、进度条使用 `role="progressbar"` |

---

## 12. 组件 Props 协议

### 12.1 MessageBubble props

```typescript
interface MessageBubbleProps {
  message: ChatMessage;  // 消息对象（含 type/timestamp）
  model?: string;        // model 名（ReplyBubble 底部显示）
}
```

### 12.2 AskCard props

```typescript
interface AskCardProps {
  askId: string;
  kind: 'confirm' | 'choice' | 'multi_choice' | 'text' | 'plan';
  question: string;
  options?: { label: string; value: any }[];  // confirm/choice/multi_choice
  multiline?: boolean;                         // text
  planSteps?: PlanStep[];                      // plan
  estimatedTime?: number;                      // plan (秒)
  onReply: (askId: string, value: any) => void;
}
```

### 12.3 ToolCard props

```typescript
interface ToolCardProps {
  toolCallId: string;
  name: string;
  status: 'running' | 'done' | 'failed';
  step: number;
  total: number;
  message: string;
  data?: any;        // result data（论文列表等）
}
```

### 12.4 PlanTodoList props

```typescript
interface PlanTodoListProps {
  phases: PlanPhase[];
}
interface PlanPhase {
  id: string;
  title: string;
  steps: PlanStep[];
  status: 'pending' | 'running' | 'done' | 'failed';
}
interface PlanStep {
  id: string;
  name: string;
  toolCallId?: string;  // 关联的 tool_call_id
  status: 'pending' | 'running' | 'done' | 'failed';
  progress?: { step: number; total: number };
}
```

### 12.5 ChatInput props

```typescript
interface ChatInputProps {
  disabled: boolean;
  onSend: (text: string, files: UploadedFile[]) => void;
}
```

---

> 下一份文档：[验收文档](./vue-acceptance-criteria.md)
