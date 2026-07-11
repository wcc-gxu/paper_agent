# 智驭·研 Vue 前端 — 产品文档

> 对应后端重构方案 v3 | 技术选型：Vue 3 + TypeScript + Vite + Naive UI + Pinia
>
> 日期：2026-07-11

---

## 目录

1. [产品概述](#1-产品概述)
2. [技术架构](#2-技术架构)
3. [页面路由与导航](#3-页面路由与导航)
4. [Chat 主页面](#4-chat-主页面)
5. [论文管理页面](#5-论文管理页面)
6. [知识库浏览页面](#6-知识库浏览页面)
7. [术语词表页面](#7-术语词表页面)
8. [任务管理页面](#8-任务管理页面)
9. [写作编辑页面](#9-写作编辑页面)
10. [设置页面](#10-设置页面)
11. [认证与多用户](#11-认证与多用户)
12. [文件服务](#12-文件服务)
13. [WebSocket 通信](#13-websocket-通信)
14. [Vue 端本地 Tool 体系](#14-vue-端本地-tool-体系)

---

## 1. 产品概述

### 1.1 定位

智驭·研 Vue 前端是面向**课题组全员（硕士生 + 导师）**的 AI 科研助理 Web 应用，部署于**课题组内网服务器**，通过 WebSocket 与后端 Agent 系统实时通信。

### 1.2 核心体验目标

- **一套代码，全端响应式**：从手机 Safari 到桌面 Chrome，同一布局自适应
- **仿 ChatGPT 交互范式**：左侧会话列表 + 中央对话区，零学习成本
- **实时进度可见**：子 Agent / Tool 执行进度实时更新，Plan 步骤状态一目了然
- **文件随手传**：支持 PDF/图片/音频/文本文件上传，仿 ChatGPT 附件交互

### 1.3 用户角色

| 角色 | 权限 |
|------|------|
| 普通用户（researcher） | 聊天、论文管理、术语查看、写作、上传文件 |
| 管理员（admin） | 以上全部 + 系统设置、用户管理 |

---

## 2. 技术架构

### 2.1 整体架构

```
┌─ 浏览器 ──────────────────────────────────────────────────────┐
│  Vue 3 SPA (Vite build)                                       │
│  ├── Naive UI (组件库)                                         │
│  ├── Pinia (状态管理：authStore / chatStore / paperStore / ...) │
│  ├── marked + KaTeX (Markdown + LaTeX 渲染)                    │
│  └── 原生 WebSocket (实时通信)                                   │
└────────────────────────────────────────────────────────────────┘
    │                    │                    │
    │ WebSocket          │ REST               │ REST
    │ ws://host:8000     │ http://host:8000   │ http://host:8001
    ▼                    ▼                    ▼
┌───────────┐   ┌───────────────┐   ┌──────────────┐
│ 主后端     │   │ 主后端 API    │   │ 文件服务      │
│ FastAPI   │   │ /api/*        │   │ FastAPI:8001 │
│ :8000     │   │               │   │ /api/files/* │
└───────────┘   └───────────────┘   └──────────────┘
```

- 前后端**严格分离**，独立部署，支持跨域（CORS）
- 生产环境通过 nginx 反代统一入口，或独立域名

### 2.2 技术栈明细

| 层 | 技术 | 说明 |
|----|------|------|
| 框架 | Vue 3 (Composition API + `<script setup>`) | — |
| 语言 | TypeScript | 严格模式 |
| 构建 | Vite | 开发 HMR + 生产 build |
| UI | Naive UI | 中文本地化好，组件齐全 |
| 状态 | Pinia | 模块化 store |
| 路由 | Vue Router | history mode |
| HTTP | fetch / axios | 带 JWT 拦截器 |
| WebSocket | 原生 WebSocket API | 心跳 + 断线重连 |
| Markdown | marked | 自定义 renderer |
| 数学公式 | KaTeX | 自动渲染 `$...$` / `$$...$$` |
| 图表 | ECharts | 研究方向聚类可视化 |

### 2.3 目录结构

```
frontend/
├── index.html
├── vite.config.ts
├── tsconfig.json
├── package.json
├── public/
│   └── favicon.ico
└── src/
    ├── main.ts                     # 入口
    ├── App.vue                     # 根组件
    ├── router/
    │   └── index.ts               # 路由配置
    ├── stores/
    │   ├── auth.ts                # 认证状态
    │   ├── chat.ts                # 聊天状态（消息列表/流式）
    │   ├── session.ts             # 会话列表
    │   ├── papers.ts              # 论文数据
    │   ├── glossary.ts            # 术语数据
    │   └── tasks.ts               # 任务状态
    ├── composables/
    │   ├── useWebSocket.ts        # WS 连接管理
    │   ├── useAuth.ts             # 登录/Token 刷新
    │   ├── useFileUpload.ts       # 文件上传
    │   └── useNotification.ts     # 桌面通知
    ├── views/
    │   ├── LoginView.vue          # 登录页
    │   ├── ChatView.vue           # 聊天主界面（首页）
    │   ├── PapersView.vue         # 论文管理
    │   ├── KnowledgeView.vue      # 知识库浏览
    │   ├── GlossaryView.vue       # 术语词表
    │   ├── TasksView.vue          # 任务管理
    │   ├── WritingView.vue        # 写作编辑器
    │   └── SettingsView.vue       # 设置
    ├── components/
    │   ├── layout/
    │   │   ├── AppLayout.vue      # 全局布局（侧边栏 + 主内容）
    │   │   ├── Sidebar.vue        # 左侧会话列表
    │   │   └── MobileNav.vue      # 移动端底部导航
    │   ├── chat/
    │   │   ├── ChatInput.vue      # 输入框 + 附件
    │   │   ├── MessageList.vue    # 消息列表
    │   │   ├── MessageBubble.vue  # 单条消息气泡
    │   │   ├── StatusBubble.vue   # 状态提示气泡
    │   │   ├── ReplyBubble.vue    # LLM 回复气泡（Markdown 渲染）
    │   │   ├── ToolCard.vue       # 子 Agent/Tool 进度卡片
    │   │   ├── AskCard.vue        # 用户交互卡片（5 形态）
    │   │   ├── PlanTodoList.vue   # Plan 步骤完成状态
    │   │   └── FileCard.vue       # 文件/图片附件卡片
    │   ├── paper/
    │   │   ├── PaperCard.vue      # 论文卡片
    │   │   ├── PaperSearchResult.vue # 搜索结果卡片组
    │   │   └── PaperFilter.vue    # 筛选栏
    │   ├── writing/
    │   │   └── MdEditor.vue       # Markdown 编辑器
    │   ├── glossary/
    │   │   └── GlossaryTable.vue  # 术语表格
    │   └── shared/
    │       ├── CitationBadge.vue  # 引用标记 badge
    │       ├── FileUploader.vue   # 文件上传组件
    │       └── WelcomeBanner.vue  # 首次登录欢迎引导
    ├── services/
    │   ├── api.ts                 # REST API 封装
    │   ├── ws.ts                  # WebSocket 连接类
    │   ├── fileService.ts         # 文件服务 API
    │   └── localTools.ts          # Vue 端本地 tool 定义
    ├── types/
    │   ├── message.ts             # WS 消息类型
    │   ├── paper.ts               # 论文类型
    │   ├── glossary.ts            # 术语类型
    │   └── tool.ts                # Tool 相关类型
    └── utils/
        ├── markdown.ts            # Markdown 渲染器（marked + KaTeX 集成）
        ├── jwt.ts                 # JWT 解析
        └── format.ts              # 日期/数字格式化
```

---

## 3. 页面路由与导航

### 3.1 路由表

| 路径 | 页面 | 说明 | 需登录 |
|------|------|------|:---:|
| `/login` | LoginView | 登录页 | 否 |
| `/` | ChatView | 聊天主界面（默认首页） | 是 |
| `/chat/:sessionId` | ChatView | 指定会话的聊天 | 是 |
| `/papers` | PapersView | 论文管理 | 是 |
| `/knowledge` | KnowledgeView | 知识库树形浏览 | 是 |
| `/glossary` | GlossaryView | 术语词表 | 是 |
| `/tasks` | TasksView | 任务管理 | 是 |
| `/writing` | WritingView | 写作编辑器 | 是 |
| `/writing/:docId` | WritingView | 编辑指定文档 | 是 |
| `/settings` | SettingsView | 设置 | 是 |

### 3.2 导航结构（仿 ChatGPT）

**桌面端**：
```
┌──┬──────────────────────────┐
│左│  顶部栏（面包屑 + 操作）    │
│侧│──────────────────────────│
│边│                          │
│栏│  主内容区                  │
│  │                          │
│  │                          │
└──┴──────────────────────────┘
```

- 左侧边栏：会话列表 + 导航入口（论文/知识库/术语/设置）
- 可一键折叠/展开（汉堡按钮）
- Chat 页面：无顶部栏，消息列表占满高度

**移动端**：
```
┌─────────────────────┐
│ 主内容区              │
│                     │
│                     │
├─────────────────────┤
│ 💬   📄   📚   ⚙   │  ← 底部 Tab 导航
└─────────────────────┘
```

- 底部 4 个 Tab：聊天、论文、设置、更多
- 左侧边栏变为抽屉式（左滑唤出）
- Safari + Chrome 一致体验

---

## 4. Chat 主页面

### 4.1 页面结构

```
┌─ ChatView ──────────────────────────────────────────────────┐
│ ┌─ Sidebar ───────────────────────────────────────────────┐ │
│ │ [智驭·研 logo]                                  [折叠]  │ │
│ │ [+ 新会话]                                              │ │
│ │ ─────────────────────────────────────────────────────── │ │
│ │ 📝 自动驾驶对抗攻击调研             2026-07-10          │ │
│ │ 📝 transformer 综述                 2026-07-09          │ │
│ │ 📝 今日arXiv速览                     2026-07-08          │ │
│ │ ...                                                     │ │
│ │ ─────────────────────────────────────────────────────── │ │
│ │ 📄 论文管理                                              │ │
│ │ 📚 知识库                                                │ │
│ │ 📖 术语词表                                              │ │
│ │ 📋 任务管理                                              │ │
│ │ ✏️ 写作编辑                                              │ │
│ │ ⚙ 设置                                                  │ │
│ └────────────────────────────────────────────────────────┘ │
│                                                            │
│ ┌─ MessageList ──────────────────────────────────────────┐ │
│ │                                                        │ │
│ │  [用户] 帮我调研自动驾驶对抗攻击，50篇                      │ │
│ │                                                        │ │
│ │  [Bot] 收到，正在分析...                                  │ │
│ │                                                        │ │
│ │  ┌─ AskCard (kind=plan) ───────────────────────────┐  │ │
│ │  │ 📋 方案概要                                       │  │ │
│ │  │ 搜 50 篇 → 筛选 → 下载 → 入库 → 综述             │  │ │
│ │  │ 预估时间：约 20 分钟                              │  │ │
│ │  │ [批准执行]  [拒绝]                                │  │ │
│ │  └────────────────────────────────────────────────┘  │ │
│ │                                                        │ │
│ │  ┌─ PlanTodoList ──────────────────────────────────┐  │ │
│ │  │ ✅ Phase 1: 文献搜索          (2/2)              │  │ │
│ │  │ 🔄 Phase 2: 论文下载入库      (1/2)              │  │ │
│ │  │ ⏳ Phase 3: 综述生成                             │  │ │
│ │  └────────────────────────────────────────────────┘  │ │
│ │                                                        │ │
│ │  ┌─ ToolCard (literature_search) ──────────────────┐  │ │
│ │  │ 📊 文献搜索                           (28/50)    │  │ │
│ │  │ ████████████░░░░░░░░ 56%                         │  │ │
│ │  └────────────────────────────────────────────────┘  │ │
│ │                                                        │ │
│ │  [Bot] ## 综述生成完毕                                │ │
│ │  Adversarial attacks pose a significant threat        │ │
│ │  [local:pap-001] ✓ ...                                │ │
│ │                                                        │ │
│ └────────────────────────────────────────────────────────┘ │
│                                                            │
│ ┌─ ChatInput ────────────────────────────────────────────┐ │
│ │ [+]  │ 输入你的问题...                    │ 发送 ▶ │   │ │
│ └────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

### 4.2 消息气泡类型

| 气泡类型 | WS type | 渲染 |
|----------|---------|------|
| **User 消息** | `message` (inbound) | 右对齐气泡，显示 user 文本 + 附件预览 |
| **Status 气泡** | `status` | 小字灰色，居中，"收到，正在分析..."（新 status 替换旧的） |
| **Reply 气泡** | `message/reply` | 左对齐气泡，Markdown 渲染正文 + 引用 Badge |
| **Tool Card** | `tool/start → progress → result` | 卡片式，含进度条 + 状态标签 |
| **Ask Card** | `ask` | 卡片式，含按钮/选择器/输入框/方案审批 |
| **Error 气泡** | `error` | 红色左对齐，无按钮 |

### 4.3 特殊渲染

#### 引用标记 Badge

```vue
<!-- CitationBadge.vue -->
<!-- [local:pap-001#sec-3.2] ✓ → 绿色 badge "pap-001" -->
<!-- [ext:10.1145/xxx] ❌ → 红色 badge "10.1145/xxx" -->
<!-- [Agent 综合] → 灰色 badge "综合" -->
```

当前版本**不可点击**，仅颜色区分。后续版本支持点击跳转论文详情。

#### LaTeX 公式

- `$...$` 行内公式 → KaTeX 渲染
- `$$...$$` 块级公式 → KaTeX 渲染
- 渲染失败 → 回退为原始文本

#### 论文搜索结果卡片

`tool/result` 的 `data` 字段含 `papers[]` 数组时，渲染为可展开的论文卡片组：

```json
{
  "type": "tool",
  "subType": "result",
  "payload": {
    "tool_call_id": "t1",
    "status": "done",
    "summary": "找到 32 篇论文",
    "data": {
      "papers": [
        {
          "id": "pap-001",
          "title": "Adversarial Attacks on Autonomous Driving",
          "authors": ["Alice Chen", "Bob Wang"],
          "year": 2024,
          "venue": "CVPR",
          "abstract": "...",
          "citation_count": 45,
          "tl_dr": "..."
        }
      ],
      "total": 32
    }
  }
}
```

前端渲染为：可折叠列表，每项显示标题/作者/年份/会议/TLDR，展开后显示摘要。

### 4.4 输入区域

```
┌──────────────────────────────────────────────────┐
│ [+] │ 输入框（多行自适应）              │ 发送 ▶ │
└──────────────────────────────────────────────────┘
```

- `[+]`：附件按钮 → 弹出文件选择器（PDF/图片/音频/文本）
- 已选文件显示为缩略图标签（可删除）
- Enter 发送，Shift+Enter 换行
- 发送中显示加载动画，按钮禁用

### 4.5 欢迎引导

新用户首次登录或新 Agent 首次对话：

```
┌─ WelcomeBanner ──────────────────────────────────┐
│                                                  │
│   👋 欢迎使用智驭·研！                               │
│                                                  │
│   我是你的 AI 科研助理，可以帮你：                      │
│                                                  │
│   🔍 文献调研    "帮我调研自动驾驶对抗攻击"              │
│   📝 综述生成    "写一篇 transformer 综述"            │
│   📚 知识库问答  "我的论文中用的什么方法"               │
│   ✏️ 写作辅助    "帮我写 related work"               │
│   📎 上传论文    "快速上传 PDF 构建知识库"             │
│                                                  │
│   请输入你的第一个任务，或直接上传论文 →                    │
│                                                  │
└──────────────────────────────────────────────────┘
```

发送第一条消息后欢迎横幅消失，进入正常对话。

---

## 5. 论文管理页面

### 5.1 页面结构

```
┌─ PapersView ───────────────────────────────────────┐
│ 搜索: [____________] [搜索]  筛选: [领域 ▼] [年份 ▼]  │
│                                                    │
│ ┌─ PaperCard ┐ ┌─ PaperCard ┐ ┌─ PaperCard ┐      │
│ │ 标题        │ │ 标题        │ │ 标题        │      │
│ │ 作者/年份    │ │ 作者/年份    │ │ 作者/年份    │      │
│ │ venue       │ │ venue       │ │ venue       │      │
│ │ TLDR        │ │ TLDR        │ │ TLDR        │      │
│ │ [展开] [删] │ │ [展开] [删] │ │ [展开] [删] │      │
│ └────────────┘ └────────────┘ └────────────┘      │
│                                                    │
│ 显示 1-20 / 共 320 篇    [上一页] [下一页]           │
└────────────────────────────────────────────────────┘
```

### 5.2 功能

- REST API：`GET /api/papers?user_id=xxx&page=1&query=...`
- 卡片网格布局（桌面 3 列，平板 2 列，手机 1 列）
- 展开显示完整摘要 + 关键词 + 引用数
- 支持删除论文（从知识库移除）
- 批量选择操作（P2）

---

## 6. 知识库浏览页面

### 6.1 页面结构

```
┌─ KnowledgeView ────────────────────────────────────┐
│ ┌─ 树形目录 ────────────┐ ┌─ 内容区 ───────────────┐│
│ │                       │ │                        ││
│ │ 📁 计算机视觉 (45)     │ │ 选中节点时显示：         ││
│ │   ├─ 📄 paper1.md     │ │ - 论文 Markdown 预览    ││
│ │   ├─ 📄 paper2.md     │ │ - Chunk 列表           ││
│ │   └─ 📄 ...           │ │ - 元数据               ││
│ │ 📁 NLP (32)           │ │                        ││
│ │ 📁 系统 (18)          │ │                        ││
│ │                       │ │                        ││
│ └───────────────────────┘ └────────────────────────┘│
└────────────────────────────────────────────────────┘
```

### 6.2 功能

- REST API：`GET /api/knowledge/tree?user_id=xxx`
- 左树右详情的经典布局
- 按领域/会议/年份分组
- 点击论文节点 → 右侧显示 Markdown 全文预览

---

## 7. 术语词表页面

### 7.1 页面结构

```
┌─ GlossaryView ──────────────────────────────────────┐
│ 搜索: [____________]  领域: [全部 ▼]  排序: [频率 ▼]  │
│                                                     │
│ ┌──────────────────────────────────────────────────┐│
│ │ 英文术语          │ 中文翻译   │ 频率 │ 置信度 │领域 ││
│ ├──────────────────────────────────────────────────┤│
│ │ adversarial attack│ 对抗攻击   │  15  │ 0.95  │ CV ││
│ │ transformer       │ 变换器     │  12  │ 0.98  │ NLP││
│ │ ...               │ ...       │  ..  │ ...   │ .. ││
│ └──────────────────────────────────────────────────┘│
│                                                     │
│ 共 87 个术语                                         │
└─────────────────────────────────────────────────────┘
```

### 7.2 功能

- REST API：`GET /api/glossary?user_id=xxx`
- 纯展示，**不提供编辑**（术语由 Glossary Sub-Agent 自动维护）
- 搜索/按领域筛选/按频率排序
- 术语超过 180 天未出现 → 置信度半透明显示

---

## 8. 任务管理页面

### 8.1 页面结构

```
┌─ TasksView ─────────────────────────────────────────┐
│ 状态: [全部 ▼]                                      │
│                                                     │
│ ┌──────────────────────────────────────────────────┐│
│ │ 任务名称         │ 状态 │ 进度 │ 时间              ││
│ ├──────────────────────────────────────────────────┤│
│ │ 自动驾驶对抗攻击   │ ✅   │ 100% │ 2026-07-10 14:30││
│ │ Transformer 综述  │ 🔄  │ 65%  │ 2026-07-09 10:15││
│ │ arXiv 每日速览    │ ❌  │ —    │ 2026-07-08 08:00││
│ └──────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
```

### 8.2 功能

- REST API：`GET /api/tasks?user_id=xxx`
- 任务状态：pending / running / done / failed
- 点击展开 → 显示 step 明细
- 支持取消运行中的任务

---

## 9. 写作编辑页面

### 9.1 页面结构

```
┌─ WritingView ───────────────────────────────────────┐
│ ┌─ 工具栏 ─────────────────────────────────────[保存]│
│ │ [B] [I] [H1] [H2] [引用] [AI味检查] [模板]        │
│ ├───────────────────────────────────────────────────┤
│ │                                                   │
│ │  # Related Work                                  │
│ │                                                   │
│ │  Adversarial attacks [local:pap-001] ✓ ...        │
│ │                                                   │
│ │  ## 子领域1                                       │
│ │  ...                                             │
│ │                                                   │
│ │                                                   │
│ ├───────────────────────────────────────────────────┤
│ │ 字符数: 1,234  | 段落: 5  | AI味问题: 2           │
│ └───────────────────────────────────────────────────┘
```

### 9.2 功能

- 大文本框 + Markdown 语法高亮（不做富文本，不做分栏预览）
- 工具栏插入 Markdown 语法（**粗体**/# 标题/`[local:xxx]` 引用）
- **[AI 味检查]** 按钮 → 后端 Writing Agent → 高亮标记问题文本
- **[模板]** 按钮 → 从模板库选择 → 插入模板骨架
- **[保存]** → 写入后端

### 9.3 与 Chat 的联动

Chat 中 LLM 生成的综述/Related Work → 用户说"复制到编辑器" → 通过 `document_create` tool 创建文档 → 自动跳转 WritingView。

---

## 10. 设置页面

### 10.1 配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| API Base URL | 后端服务地址 | 部署时写入配置文件 |
| 默认 LLM 模型 | doubao / 火山引擎 / 自定义 | doubao |
| 桌面通知 | 开/关 | 开 |
| 主题 | 浅色 / 深色 / 跟随系统 | 跟随系统 |
| 语言 | 中文 / English | 中文 |
| 论文存储路径 | 服务器端路径（管理员可见） | ~/papers |

---

## 11. 认证与多用户

### 11.1 JWT 认证流程

```
1. 用户打开页面 → 未登录 → 重定向 /login
2. 输入 username + password → POST /api/auth/login
3. 后端返回 { access_token, refresh_token, expires_in }
4. 前端存 localStorage:
     auth_token: access_token
     refresh_token: refresh_token
     user: { username, display_name, role }
5. 所有 REST 请求: Authorization: Bearer <access_token>
6. WebSocket 连接: ws://host/ws/chat/{agent_id}/{session_id}?token=<access_token>
7. access_token 过期 → POST /api/auth/refresh → 新 access_token
8. refresh_token 也过期 → 跳转 /login
```

### 11.2 权限控制

- 路由守卫：未登录 → `/login`
- API 拦截器：401 → 自动刷新 token 或跳转登录
- 管理员路由：`role=admin` 才可见

---

## 12. 文件服务

### 12.1 独立文件服务

```
文件服务：FastAPI :8001
  POST /api/files/upload      → 上传文件 → { file_id, url, name, size, mime }
  GET  /api/files/{id}        → 文件下载
  GET  /api/files/{id}/meta   → 文件元信息
  DELETE /api/files/{id}      → 删除文件

支持类型：PDF (.pdf) / 图片 (.png/.jpg/.webp) / 音频 (.mp3/.wav/.m4a) / 文本 (.txt/.md)
单文件限制：50MB
```

### 12.2 上传交互（仿 ChatGPT）

```
用户点击输入框 [+] 按钮
  → 弹出系统文件选择器（accept=".pdf,.png,.jpg,.jpeg,.webp,.mp3,.wav,.m4a,.txt,.md"）
  → 选择文件后：
     - 图片 → 自动显示缩略图预览（在输入框上方，可删除）
     - PDF → 显示文件卡片（文件名 + 大小，可删除）
     - 音频 → 显示文件卡片（文件名 + 时长，可删除）
  → 用户输入文字 + 点击发送
  → 前端先上传文件到 /api/files/upload（并行上传多文件）
  → 上传完成后发送 WS message，payload.files 附带文件信息
  → Chat 中用户消息气泡内渲染附件卡片
```

---

## 13. WebSocket 通信

### 13.1 连接管理

- 连接地址：`ws://{host}:8000/ws/chat/{agent_id}/{session_id}?token={jwt}`
- 心跳：每 30 秒 ping，60 秒无响应断开重连
- 断线重连：指数退避（1s → 2s → 4s → 8s → 最大 30s）
- 重连后发送 `sync` 拉取离线消息

### 13.2 消息处理（前端）

| WS type | 前端处理 |
|---------|----------|
| `pong` | 更新心跳时间戳 |
| `status` | 追加/替换 StatusBubble |
| `message/reply` | 追加 ReplyBubble（Markdown 渲染） |
| `tool/start` | 新增 ToolCard 或 PlanTodoList 步骤 |
| `tool/progress` | 更新对应 ToolCard 进度/消息 |
| `tool/result` | 标记完成/失败，展开结果 data |
| `tool/call` | 执行 Vue 本地 tool → 回 `tool_result` |
| `ask` | 渲染 AskCard（根据 kind 切换 5 形态） |
| `error` | 渲染 ErrorBubble |
| `sync_complete` | 标记同步完成 |

### 13.3 消息发送（前端）

| Inbound type | 触发场景 |
|-------------|----------|
| `ping` | 心跳定时器 |
| `message` | 用户发送文本 + 附件 |
| `ask_reply` | 用户操作 AskCard |
| `tool_result` | Vue 本地 tool 执行完毕 |
| `sync` | WS 重连后 |

### 13.4 Capabilities 上报

```json
// 每条 inbound message 附带
{
  "capabilities": [
    "ask_user_question",
    "document_create",
    "document_edit",
    "cloud_sync",
    "file_upload",
    "file_preview",
    "clipboard_copy",
    "desktop_notification",
    "settings"
  ]
}
```

---

## 14. Vue 端本地 Tool 体系

### 14.1 Tool 定义

| tool_name | capability | 触发方式 | 行为 |
|-----------|-----------|----------|------|
| `ask_user_question` | 通用 | WS `tool/call` | 渲染 AskCard（5 种 kind），等用户操作后回 `tool_result` |
| `document_create` | 通用 | WS `tool/call` | 创建新文档 → 返回 doc_id → 可跳转 WritingView |
| `document_edit` | 通用 | WS `tool/call` | 打开已有文档 → 等用户编辑保存后回 `tool_result` |
| `cloud_sync` | 通用 | WS `tool/call` | 触发同步到 Zotero/Overleaf（P3） |
| `file_upload` | 通用 | 用户点 [+] 或 WS `tool/call` | 打开文件选择器 → 上传 |
| `file_preview` | 通用 | WS `tool/call` 或用户点击 | 预览文件/图片 |
| `clipboard_copy` | 通用 | WS `tool/call` | 复制指定内容到系统剪贴板 |
| `desktop_notification` | 通用 | 长任务完成 | 弹桌面通知 |
| `settings` | 通用 | 用户点击 ⚙ | 打开设置面板 |

### 14.2 AskUserQuestion 作为通用交互 Tool

这是 Vue 端**最关键的本地 tool**，统一处理所有用户交互场景：

```
后端 WS: tool/call { name: "ask_user_question", input: { ... } }
    │
    ▼
前端: 根据 input 渲染对应 AskCard 形态
    │
    ├── kind=confirm    → [确认] [取消] 按钮
    ├── kind=choice     → 单选列表
    ├── kind=multi_choice → 多选 chips
    ├── kind=text       → 文本输入框
    └── kind=plan       → 方案审批（步骤列表 + 权限 + 预估时间）
    │
用户操作
    │
    ▼
前端: ask_reply { ask_id, value }
```

**AskCard 通用性设计**：一个组件承载 5 种交互形态，通过 `kind` prop 切换，减少代码重复，确保交互一致性。

---

> 下一份文档：[设计/交互文档](./vue-design-spec.md)
