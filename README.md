<div align="center">


# 🌟 SAG

**SQL驱动的RAG引擎 · 查询时自动构建知识图谱**

*The SQL-Driven Smart Auto Graph Engine*

[![zread](https://img.shields.io/badge/Ask_Zread-_.svg?style=flat-square&color=00b0aa&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff)](https://zread.ai/Zleap-AI/SAG)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Next.js-14-black?style=flat-square&logo=next.js&logoColor=white)](https://nextjs.org)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue?style=flat-square)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](CONTRIBUTING.md)

**by [Zleap.AI](https://zleap.ai)** · Open Source · Production Ready

<a href="README_en.md">English</a> | <a href="README.md">中文</a>

</div>

---


https://github.com/user-attachments/assets/e03567a5-2cc4-4d09-aab9-01277d8d4457


---

## 🌟 SAG 是什么？

> [入门版原理介绍](https://mp.weixin.qq.com/s/dmrLphM3bLBC2Nj1xsfdHg)

**SAG**是SQL驱动的新一代 RAG 引擎，可以在查询时自动构建知识图谱

  - 把原始文本自动拆解成“**语义原子事件**”  
  - 为每个事件抽取多维“**自然语言向量（多维度实体）**”  
  - 在 **查询时动态构建关系网络**，而不是预先维护知识图谱

### 核心能力

- **自动理解**：AI自动将文档拆解为原子性的事件
- **智能关联**：检索时动态构建关系网络，无需预先维护图谱
- **精准召回**：三阶段搜索（Recall → Expand → Rerank），找到最相关的信息
- **完整溯源**：每个结果都能追溯来源和关联链路
- **灵活扩展**：支持自定义实体类型，适配任何业务场景

### 适合谁？
  - 👨‍💻 **普通开发者**：想要一个好用、易部署、可定制的本地 / 企业 RAG 引擎  
  - 🏢 **企业技术团队**：需要可审计、可控、私有化部署的知识中台  
  - 🧑‍🔬 **研究人员**：对 GraphRAG / RAG+KG 感兴趣，想深入算法与数学分析

---

## 💡 应用场景

SAG 最初来源于一个核心问题：**如何在不维护庞大知识图谱的前提下，让机器真正“理解”和“关联”海量文本？**

- **从产品视角**：一个可以托管你所有文档、对话、业务数据的“**数据智能引擎**”  
- **从技术视角**：一种 **Event-Centric 的动态知识图谱构建算法**，在查询时按需生成图结构  
- **从实现视角**：组合 **SQL 精确检索 + 向量语义搜索 + PageRank** 的三阶段搜索系统


<table>
<tr>
<td width="33%">


#### 📚 个人知识管理

```
痛点：
• 笔记分散，难以查找
• 信息孤岛，关联性差
• 手动整理费时费力

解决：
✓ 自动拆分知识卡片
✓ 智能标注实体关系
✓ 秒级多维度检索
```

</td>
<td width="33%">

#### 👥 团队协作文档

```
痛点：
• 版本混乱，信息冗余
• 决策分散，难以追溯
• 新人上手成本高

解决：
✓ 自动提取决策点
✓ 追踪信息演进
✓ 快速生成报告
```

</td>
<td width="33%">

#### 🔬 研究分析助手

```
痛点：
• 文献量大，难抽重点
• 手动标注耗时长
• 关系发现靠直觉

解决：
✓ 自动提取论点
✓ 构建主题网络
✓ 发现隐含关联
```

</td>
</tr>
</table>


### 与传统方案对比

|              | 传统RAG  | GraphRAG | SAG      |
| ------------ | -------- | -------- | -------- |
| **数据组织** | 固定切块 | 预构建图 | 事件化   |
| **关系维护** | 无       | 静态存储 | 动态计算 |
| **扩展性**   | ⭐⭐       | ⭐⭐⭐      | ⭐⭐⭐⭐⭐    |
| **维护成本** | 低       | 高       | 低       |
| **检索精度** | ⭐⭐       | ⭐⭐⭐⭐     | ⭐⭐⭐⭐⭐    |
| **适用场景** | 简单问答 | 深度问答 | 全场景   |

---

## 🎯 核心特性

### 1. 系统架构一览


SAG 的系统设计直接对应其算法设计：**存储时“事件化”，查询时“图谱化”**。

```text
┌─────────────────────────────────────────────┐
│                Data Processing 数据处理     │
│  LOAD   ─→   EXTRACT   ─→   INDEX          │
│  加载        事件提取        索引（向量+SQL）│
└────────────────────┬───────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────┐
│                Storage 存储层              │
│  • MySQL: 事件 / 实体 / 事件-实体关系      │
│  • Elasticsearch / VecDB: 向量检索         │
└────────────────────┬───────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────┐
│               Search 检索层                │
│  Recall  ─→  Expand  ─→  Rerank            │
│  实体召回      多跳扩展      智能排序       │
└─────────────────────────────────────────────┘
```

### 2. 灵活的实体维度系统

**系统默认（5W1H）**：

| 维度       | 含义 | 权重 | 示例                              |
| ---------- | ---- | ---- | --------------------------------- |
| 🕐 TIME     | 时间 | 0.9  | "2024年6月", "每周一", "昨天下午" |
| 📍 LOCATION | 地点 | 1.0  | "会议室A", "北京", "线上群聊"     |
| 👤 PERSON   | 人员 | 1.1  | "张三", "产品经理", "客户"        |
| 🎯 TOPIC    | 话题 | 1.5  | "大模型优化", "项目延期"          |
| ⚡ ACTION   | 行为 | 1.2  | "决定", "完成", "优化"            |
| 🏷️ TAGS     | 标签 | 1.0  | "技术", "紧急", "待跟进"          |

**自定义扩展**：

```python
# 项目管理场景
custom_entities = [
    EntityType(type="project_stage", name="项目阶段", weight=1.2),
    EntityType(type="risk_level", name="风险等级", weight=1.3)
]

# 医疗场景
custom_entities = [
    EntityType(type="symptom", name="症状", weight=1.4),
    EntityType(type="diagnosis", name="诊断", weight=1.5)
]
```

### 3. 三阶段智能搜索

```
阶段1: Recall - 实体召回
├─ LLM理解查询意图
├─ 提取结构化实体
├─ 向量检索相关实体
└─ 定位关联事件
    
阶段2: Expand - 多跳扩展
├─ 从初始事件出发
├─ 通过共同实体发现关联
├─ BFS多跳探索（深度可配）
└─ 构建完整关系网络
    
阶段3: Rerank - 智能排序
├─ PageRank计算重要性
├─ 综合：实体权重+时间+相似度
├─ 方向性权重（重要事件权重大）
└─ 返回Top-N + 线索链
```

**可解释性示例**：

```json
{
  "event": "MoE架构的稀疏专家层设计",
  "score": 0.89,
  "clues": [
    {
      "stage": "recall",
      "from": "Query: 大模型优化",
      "to": "Entity: MoE架构",
      "confidence": 0.92
    },
    {
      "stage": "expand", 
      "from": "Event: MoE架构优势",
      "to": "Event: 稀疏专家层设计",
      "shared_entities": ["MoE架构", "专家层"],
      "confidence": 0.85
    }
  ]
}
```

---

## 🚀 快速开始

### 方式一：Docker Compose（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/Zleap-AI/SAG.git
cd SAG

# 2. 配置环境
cp .env.example .env
# 编辑 .env：
#   LLM_API_KEY=sk-xxx
#   MYSQL_PASSWORD=your_password

# 3. 下载资源 （首次执行）
python scripts/download_nltk_data.py

# 4. 启动服务
docker compose up -d

# 5. 访问
# 前端: http://localhost
# API: http://localhost/api/docs
```

### 方式二：Python SDK

```python
import asyncio
from sag import SAGEngine
from sag.modules.load.config import LoadBaseConfig
from sag.modules.extract.config import ExtractBaseConfig
from sag.modules.search.config import SearchBaseConfig

async def main():
    # 初始化
    engine = SAGEngine(source_config_id="my-project")
    
    # 加载文档
    await engine.load(LoadBaseConfig(
        type="path",
        origin=["./docs/article.md"],
        background="技术文档"
    ))
    
    # 提取事件
    await engine.extract(ExtractBaseConfig(
        parallel=True,
        background="AI大模型文档"
    ))
    
    # 智能检索
    result = await engine.search(SearchBaseConfig(
        query="如何优化大模型推理速度？",
        depth=2,
        top_k=10
    ))
    
    # 查看结果
    for event in result.events:
        print(f"[{event.score:.2f}] {event.title}")
        print(f"  {event.summary}\n")

asyncio.run(main())
```

### 方式三：Web界面

访问 http://localhost:3000

1. **上传文档**：拖拽 Markdown、PDF、HTML
2. **自动处理**：系统自动加载→提取→索引
3. **智能搜索**：输入自然语言查询
4. **查看结果**：浏览事件、线索图谱、来源



---

## 🌐 开源版 vs 完整版

### 功能对比

| 功能         | 开源基础版 | [完整版](https://zleap.ai) |
| ------------ | ---------- | -------------------------- |
| **核心引擎** | ✅ 完整开源 | ✅ 相同引擎                 |
| **文档加载** | ✅ 本地文件 | ✅ 多种信息源               |
| **数据源**   | ✅ 手动上传 | ✅ 自动更新                 |
| **内容发布** | ❌          | ✅ 一键生成文章/报告        |
| **协作**     | ❌ 单用户   | ✅ 团队 + 权限管理          |
| **高级功能** | ❌          | ✅ 智能推荐 + 自动摘要      |
| **云服务**   | ❌ 需自建   | ✅ 开箱即用                 |
| **支持**     | 社区       | 专业技术团队               |

### 为什么开源基础版？

我们相信：

- 🌍 **技术共享**：核心算法应该被更多人使用和改进
- 🔧 **灵活部署**：企业可自建私有化部署
- 🤝 **社区驱动**：开源社区的反馈让产品更好
- 💡 **创新激励**：开发者可基于SAG构建自己的应用

### 什么时候用完整版？

- 需要自动网页追踪和信息流管理
- 想接入更多信息源
- 需要团队协作和权限管理
- 希望零部署，开箱即用
- 需要专业技术支持

**体验完整版**：[https://zleap.ai](https://zleap.ai)



---
## 📖 深入学习

>这一节是给对算法细节感兴趣的开发者和研究人员的简版说明。

### 🧠 核心理念：Event & Natural Language Vector
SAG 的底层思想可以用两句话概括：

- **事件原子化（Event Atomization）**  
  *不再按字符/Token 长度“机械切块”，而是将文档转化为一个个 **语义完整、彼此独立** 的“事件 (Event)”。*
- **自然语言向量（Natural Language Vector）**  
  *不只把整段文本编码成向量，而是为每个 Event 抽取多维实体：时间、地点、人物、动作、话题、标签…  
  它们组成了一个“**由自然语言实体构成的向量**”。*

**关键洞察**：  
- Event 是 **原子知识单元**  
- Entity 是 **事件的实体维度**  
- 事件之间的关系不提前计算，而是 **在查询时动态计算**

### 🧮 三阶段搜索算法（Recall → Expand → Rerank）


### 1. Recall：实体驱动召回（Entity-Based Recall）

**目标**：从查询语句出发，找到一批高度相关的 **实体 + 事件**。

- **步骤概要**：
  - **LLM 解析查询**：抽取结构化实体（TOPIC、ACTION、PERSON…）
  - **向量检索实体**：在实体向量空间中搜索  
  - **用实体查事件（SQL）**：通过实体 ID 反查事件  
  - **事件向量检索**：直接在 Event 向量上查  
  - **交集过滤 + 权重反向传播**：兼顾语义相似度与实体匹配



### 2. Expand：基于 BFS 的多跳扩展

**目标**：通过“共享实体模式”在事件-实体空间做 **多跳搜索**，找到更深层的相关信息。

- **做法**：
  - 将高权重实体视作当前“前沿层”  
  - 用这些实体在 SQL 中查找新事件  
  - 对新事件计算相似度和权重，并将权重反向传播给新实体  
  - 只保留“新出现”的实体，形成下一跳前沿层  
  - 过程中带有 **权重衰减** + **去重**，无新实体时自动收敛

- **特性**：
  - 与“六度空间理论”类似：任意两个事件，往往可以通过少量中间实体连接  
  - 深度 2 通常在 **精度 / 召回 / 延迟** 上达到最优平衡
  
- **实体权重示意公式**：

$$W(k_i) = \sum_{e_j \in E} \left[ W_{e2}(e_j) \times \frac{count(k_i, e_j)}{\ln(1 + step_{ij})} \right]$$

### 3. Rerank：基于方向性 PageRank 的排序

在 Recall + Expand 得到的事件子图上，SAG 构建隐式图并运行 **加权 PageRank**：

- **节点**：事件 `e`  
- **有向边**：共享实体关系，边权由实体权重 + 频次决定：

$$
W(e_i \rightarrow e_j) = \sum_{k \in (e_i \cap e_j)} W_{\text{entity}}(k) \cdot \ln(1 + \text{freq}(k, e_j))
$$

- **PageRank 迭代**：

$$
\mathrm{PR}(e_j) = \frac{1-d}{N} + d \sum_{e_i \in \mathrm{In}(e_j)} \mathrm{PR}(e_i) \cdot \frac{W(e_i \rightarrow e_j)}{\sum\limits_k W(e_i \rightarrow e_k)}
$$

- **最终综合评分**（四因子加权）：

$$
S(e) = \alpha \cdot \mathrm{PR}(e) + \beta \cdot \mathrm{Sim}(Q, e) + \gamma \cdot \mathrm{EntityScore}(e) + \delta \cdot \mathrm{TimeDecay}(e)
$$

其中典型配置：α=0.4，β=0.3，γ=0.2，δ=0.1。

---


## 🤝 社区与贡献

### 加入我们

- 🌐 官网：[https://zleap.ai](https://zleap.ai)
- 💬 Discord：[加入讨论](https://discord.com/invite/DRCmtBJhyN)
- 📧 邮箱：contact@zleap.ai
- 🐦 Twitter：[@ZleapAI](https://x.com/zleapai)

### 如何贡献

```bash
# 1. Fork并克隆
git clone https://github.com/your-name/SAG.git

# 2. 创建分支
git checkout -b feature/amazing-feature

# 3. 提交更改
git commit -m "feat: add amazing feature"

# 4. 推送
git push origin feature/amazing-feature

# 5. 开启 Pull Request
```

**Commit规范**：`feat:` | `fix:` | `docs:` | `refactor:` | `test:` | `chore:`

### 贡献者墙




---

## 🙏 致谢

- 感谢所有贡献者
- 特别感谢[302.AI](https://302.ai)的算力支持

---

## 📄 许可证


本项目采用 [Apache-2.0 License](LICENSE)

---

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Zleap-AI/SAG&type=Date)](https://star-history.com/#Zleap-AI/SAG&Date)

---

<div align="center">


**让信息产生连接，让数据成为资产**

Made with ❤️ by [Zleap Team](https://zleap.ai)

</div>

---
