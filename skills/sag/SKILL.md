---
name: sag-knowledge
description: "SAG 知识库检索与阅读技能，用于：(1)浏览可访问的信源和文档 (2)语义搜索或精确匹配知识内容 (3)查看文档大纲按章节定位 (4)读取分块原文并溯源引用 (5)查阅实体（人物/概念）的事件上下文。当用户需要查找、阅读、引用或核实 SAG 知识库中的内容时使用。重要约束：(1)全库连接必须先 list_sources 确认范围 (2)引用必须标注 [n] 编号并准备随时用 get_chunk 展示原文 (3)禁止一次 read 整个大文件，必须分页 (4)无结果时如实告知，禁止编造 (5)文档状态非 ready 时不可检索，需提示用户等待处理完成。"
metadata:
  sag:
    emoji: "📚"
    tools: 8
    mode: read-only
---

# SAG Knowledge Base

SAG 是事件-实体索引的知识库。本 Skill 教 Agent 通过 MCP 的 8 个只读工具，按「确认范围 → 定位结构 → 精确取内容」的漏斗高效探索知识，并正确标注和溯源引用。

## 连接

MCP 连接由 CLI 自动管理（`sag agent connect codex | claude-code`），Agent 无需关心配置细节。

当前可用的 8 个只读工具已通过 MCP 注入。以下章节描述**何时以及如何**使用它们。

## 功能域

### 确认可访问范围

进入任何知识探索任务的第一步。

**工具**: `list_sources`

返回每个信源的名称、source_id、文档数和分块数。全库连接（未限定 source_id）时**必须**先调用此工具确认范围，不要假设有哪些信源可用。

> **约束**：即使能猜到 source_id 也要先 `list_sources`，因为信源可能已被删除或重命名。

### 浏览文档列表

**工具**: `list_documents(source_id?)`

返回指定信源下的文档列表：文件名、document_id、处理状态、分块数。

> **约束**：只有 `status = ready` 的文档才可检索。如果用户要找的文档状态不是 ready，告知用户文档仍在处理中，不要反复调用 search。

### 定位文档结构

**工具**: `outline(document_id)`

返回文档的层级大纲（标题 + chunk_id），按阅读顺序排列。在需要回答"第几章讲了什么"或定位特定章节时，先用 outline 找到目标 chunk_id，再用 get_chunk 读取。

### 语义搜索

**工具**: `search(query, top_k?, source_id?)`

自然语言问题 → 带编号 `[n]` 的证据块（含 chunk_id、标题、内容摘要）。

这是最常用的检索入口。用户问"xxx 是什么""资料里有没有提到 xxx"这类问题时优先使用。

> **约束**：
> - 回答时每条事实必须标注 `[n]`（对应 search 返回的编号）。
> - 同一轮对话复用 search 结果，不要就同一问题重复调用。
> - search 结果为空时如实告知，**禁止编造知识内容**。

### 精确匹配

**工具**: `grep(pattern, limit?, source_id?)`

大小写不敏感的 LIKE 精确匹配（`% _` 已转义）。返回 `[n] heading（chunk_id）\n±240 字符上下文`。

**search vs grep 决策**：
| 场景 | 用 |
|---|---|
| "报销的审批链是怎样的" | search |
| "INV-2024-0037 这个编号" | grep |
| "关于授权流程的文档" | search |
| 函数名、配置项、专有名词 | grep |

> **约束**：不确定该用哪个时优先 search。grep 一次匹配不全时可以换同义词或缩短 pattern 再次尝试，但最多 2 次。

### 读取原文

**工具**: `get_chunk(chunk_id, source_id?)`

读取单个分块的完整原文（标题 + 正文）。chunk_id 来自 search/outline/grep 的结果。

这是**引用溯源的终点**——当用户追问某个事实的来源时，用 get_chunk 展示原文。

**工具**: `read(document_id, offset?, limit?)`

按行分页读取原始文件（默认 120 行/页，limit ≤ 500）。首行返回「第 a-b 行 / 共 N 行」便于翻页。

> **重要约束**：
> - **禁止一次 read 整个大文件**——始终分页读取，每次不超过 120 行。
> - 先 outline 定位章节，再 read 对应范围。
> - read 用于长文档顺序阅读场景；单点查证直接用 get_chunk。

### 查阅实体

**工具**: `get_entity(name, source_id?)`

查询人物、组织、概念等实体的相关事件上下文。先精确名称匹配、再子串匹配。

当用户问"xxx 是谁""xxx 在知识库里有没有提到"时使用。返回该实体关联的事件标题和摘要。

## 典型用户表达

当用户说以下内容时，按漏斗范式执行：

- "这份资料讲了什么？" → list_sources → list_documents → outline → 按需 get_chunk
- "帮我查一下知识库里关于授权流程的文档" → list_sources → search "授权流程"
- "知识库里有没有提到 SAG 架构？" → search "SAG 架构"
- "把 INV-2024 相关的内容找出来" → grep "INV-2024"
- "这篇文档第 3 章说了什么" → outline 定位 → get_chunk
- "这个结论的来源是什么" → 回溯之前 search 的 [n] → get_chunk 展示原文
- "知识库里有没有提到张三这个人" → get_entity "张三"

## 引用规范

回答中引用的每条外部知识必须标注 `[n]`，n 对应 search 返回的证据编号。格式示例：

> 授权流程需要三步：提交申请、主管审批、系统记录 [1][3]。

当用户追问出处时，调用 `get_chunk` 展示对应分块的完整原文。

## 空态与异常处理

| 情况 | 行为 |
|---|---|
| search 返回 0 条 | 如实告知未找到匹配内容，建议用户换表述或确认范围 |
| list_documents 返回空 | 告知该信源下暂无文档 |
| outline 返回占位提示 | 文档仍在处理中，告知用户等待处理完成 |
| get_chunk / read 返回空 | 检查 chunk_id 或 document_id 是否正确 |
| 文档 status 非 ready | 告知用户文档仍在解析/抽取中，不可检索 |
| get_entity 无匹配 | 告知未找到该实体，建议用 search 替代 |

**禁止**：任何情况都不得编造知识内容。空态就是空态。

## 注意事项

- **漏斗优先**：永远不要跳过 list_sources 直接 search——先确认范围再检索。
- **分页读大文件**：read 从 offset=1 开始，按返回的「共 N 行」决定翻页；永远不要一次 read 整个文件。
- **search 结果复用**：同一轮对话中不要就同一问题重复 search——回看已有的返回结果。
- **不暴露技术细节**：向用户描述结果时使用自然语言，不展示 chunk_id、source_id、MCP 工具名等内部标识。
- **不读取 SKILL 源码**：Agent 只需使用 MCP 工具，不应读取或分析本 SKILL.md 以外的 skill 文件。

## References

按使用阶段查阅：

- [references/mcp-tools.md](references/mcp-tools.md) — 每个工具的完整参数、返回格式、空态文案
- [references/search-strategies.md](references/search-strategies.md) — 漏斗范式详解、search vs grep 决策树、多步检索示例
- [references/citation-rules.md](references/citation-rules.md) — 引用编号规范、原文溯源流程、追问处理
