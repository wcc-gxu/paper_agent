---
name: codegraph
description: 代码结构搜索与分析 — 符号查询、调用链追踪、代码探索、依赖分析。优先于 grep/glob 用于代码搜索场景。触发词包括：查代码结构、调用链、谁调用了、定义位置、代码依赖、搜索符号、explore。
allowed-tools: [Bash]
---

# CodeGraph — 代码结构搜索与分析

本地 CPG（Code Property Graph）代码索引工具，已初始化，覆盖项目全部 98 个 Python 文件（2517 节点 / 6197 边）。

## 使用规则

1. **代码搜索优先使用 codegraph**：查询符号定义、调用关系、依赖分析等场景，优先用 codegraph CLI，而非 grep/glob
2. **grep/glob 仍可用于 codegraph 不擅长的场景**：搜索注释内容、字符串常量、配置文件等非代码结构查询

## 核心命令

| 命令 | 用途 | 示例 |
|------|------|------|
| `codegraph query <keyword>` | 模糊搜索符号/文件/导入 | `codegraph query "MainAgent"` |
| `codegraph explore <query>` | 深入探索代码区域（含源码 + 调用链） | `codegraph explore "execute evaluate"` |
| `codegraph node <symbol>` | 单个符号的源码 + caller/callee | `codegraph node "MainGraph"` |
| `codegraph callers <symbol>` | 查找指定符号的所有调用者 | `codegraph callers "build_main_graph"` |
| `codegraph files` | 项目文件结构 | `codegraph files` |
| `codegraph status` | 索引状态统计 | `codegraph status` |
| `codegraph index` | 重建完整索引 | `codegraph index` |
| `codegraph sync` | 增量同步变更 | `codegraph sync` |

## 使用场景

- 查找某个类/函数的定义位置 → `codegraph query`
- 理解函数实现和调用链 → `codegraph node`
- 了解代码修改的影响范围（blast radius）→ `codegraph explore`
- 查找谁在用某个 API → `codegraph callers`
- 全局搜索符号 → `codegraph query`
