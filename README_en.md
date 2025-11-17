<div align="center">


# 🌟 SAG

**SQL-Driven RAG Engine · Automatically Build Knowledge Graph During Querying**

*The SQL-Driven Smart Auto Graph Engine*

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Next.js-14-black?style=flat-square&logo=next.js&logoColor=white)](https://nextjs.org)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue?style=flat-square)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](CONTRIBUTING.md)

**by [Zleap.AI](https://zleap.ai)** · Open Source · Production Ready

[English](#english) | [中文](#chinese)


</div>

---


https://github.com/user-attachments/assets/e03567a5-2cc4-4d09-aab9-01277d8d4457


---

## 🌟 What is SAG?

> [Introduction to Basic Principles](https://medium.com/@zleapai/the-post-rag-era-is-here-sag-redefines-ai-search-3c307c786e1e)


**SAG** is a SQL-driven next-generation RAG engine that can automatically build knowledge graphs during querying

  - 🧩 Automatically decompose raw text into "**semantic atomic events**"  
  - 🧑‍💻 Extract multi-dimensional "**natural language vectors (multi-dimensional entities)**" for each event  
  - 🔗 **Dynamically construct relationship networks at query time**, rather than maintaining knowledge graphs in advance

### Core Capabilities

- 🧠 **Automatic Understanding**: AI automatically decomposes documents into atomic events
- 🔗 **Intelligent Association**: Dynamically construct relationship networks during retrieval without pre-maintaining graphs
- 🎯 **Precise Recall**: Three-stage search (Recall → Expand → Rerank) to find the most relevant information
- 📊 **Complete Traceability**: Every result can trace back to its source and association chain
- 🔧 **Flexible Extension**: Support custom entity types, adaptable to any business scenario

### Who is it for?
  - 👨‍💻 **General Developers**: Want a good, easy-to-deploy, customizable local/enterprise RAG engine  
  - 🏢 **Enterprise Tech Teams**: Need auditable, controllable, privately deployed knowledge platforms  
  - 🧑‍🔬 **Researchers**: Interested in GraphRAG / RAG+KG, want to dive deep into algorithms and mathematical analysis

---

## 💡 Use Cases

SAG originally came from a core question: **How can machines truly "understand" and "associate" massive amounts of text without maintaining huge knowledge graphs?**

- **From Product Perspective**: A "**Data Intelligence Engine**" that can host all your documents, conversations, and business data  
- **From Technical Perspective**: An **Event-Centric dynamic knowledge graph construction algorithm** that generates graph structures on-demand at query time  
- **From Implementation Perspective**: A three-stage search system combining **SQL precise retrieval + vector semantic search + PageRank**


<table>
<tr>
<td width="33%">


#### 📚 Personal Knowledge Management

```
Pain Points:
• Scattered notes, hard to find
• Information silos, poor correlation
• Manual organization is time-consuming

Solution:
✓ Automatically split knowledge cards
✓ Intelligently annotate entity relationships
✓ Multi-dimensional retrieval in seconds
```

</td>
<td width="33%">

#### 👥 Team Collaboration Documents

```
Pain Points:
• Version chaos, information redundancy
• Scattered decisions, hard to trace
• High onboarding cost for newcomers

Solution:
✓ Automatically extract decision points
✓ Track information evolution
✓ Quickly generate reports
```

</td>
<td width="33%">

#### 🔬 Research Analysis Assistant

```
Pain Points:
• Large volume of literature, hard to extract key points
• Manual annotation is time-consuming
• Relationship discovery relies on intuition

Solution:
✓ Automatically extract arguments
✓ Build topic networks
✓ Discover implicit associations
```

</td>
</tr>
</table>


### Comparison with Traditional Solutions

|                              | Traditional RAG | GraphRAG        | SAG                 |
| ---------------------------- | --------------- | --------------- | ------------------- |
| **Data Organization**        | Fixed Chunking  | Pre-built Graph | Event-based         |
| **Relationship Maintenance** | None            | Static Storage  | Dynamic Calculation |
| **Scalability**              | ⭐⭐              | ⭐⭐⭐             | ⭐⭐⭐⭐⭐               |
| **Maintenance Cost**         | Low             | High            | Low                 |
| **Retrieval Precision**      | ⭐⭐              | ⭐⭐⭐⭐            | ⭐⭐⭐⭐⭐               |
| **Applicable Scenarios**     | Simple Q&A      | Deep Q&A        | All Scenarios       |

---

## 🎯 Core Features

### 1. System Architecture Overview


SAG's system design directly corresponds to its algorithm design: **"Event-based" during storage, "graph-based" during queries**.

```text
┌─────────────────────────────────────────────┐
│                Data Processing              │
│  LOAD   ─→   EXTRACT   ─→   INDEX          │
│  Load        Event Extraction    Index (Vector+SQL)│
└────────────────────┬───────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────┐
│                Storage Layer                │
│  • MySQL: Events / Entities / Event-Entity Relations      │
│  • Elasticsearch / VecDB: Vector Retrieval         │
└────────────────────┬───────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────┐
│               Search Layer                  │
│  Recall  ─→  Expand  ─→  Rerank            │
│  Entity Recall      Multi-hop Expansion      Intelligent Ranking       │
└─────────────────────────────────────────────┘
```

### 2. Flexible Entity Dimension System

**System Default (5W1H)**:

| Dimension  | Meaning  | Weight | Examples                                           |
| ---------- | -------- | ------ | -------------------------------------------------- |
| 🕐 TIME     | Time     | 0.9    | "June 2024", "Every Monday", "Yesterday afternoon" |
| 📍 LOCATION | Location | 1.0    | "Meeting Room A", "Beijing", "Online Group Chat"   |
| 👤 PERSON   | Person   | 1.1    | "Zhang San", "Product Manager", "Customer"         |
| 🎯 TOPIC    | Topic    | 1.5    | "Large Model Optimization", "Project Delay"        |
| ⚡ ACTION   | Action   | 1.2    | "Decide", "Complete", "Optimize"                   |
| 🏷️ TAGS     | Tags     | 1.0    | "Technology", "Urgent", "To Follow Up"             |

**Custom Extension**:

```python
# Project management scenario
custom_entities = [
    EntityType(type="project_stage", name="Project Stage", weight=1.2),
    EntityType(type="risk_level", name="Risk Level", weight=1.3)
]

# Medical scenario
custom_entities = [
    EntityType(type="symptom", name="Symptom", weight=1.4),
    EntityType(type="diagnosis", name="Diagnosis", weight=1.5)
]
```

### 3. Three-Stage Intelligent Search

```
Stage 1: Recall - Entity Recall
├─ LLM understands query intent
├─ Extract structured entities
├─ Vector retrieval of related entities
└─ Locate associated events
    
Stage 2: Expand - Multi-hop Expansion
├─ Start from initial events
├─ Discover associations through shared entities
├─ BFS multi-hop exploration (configurable depth)
└─ Build complete relationship network
    
Stage 3: Rerank - Intelligent Ranking
├─ PageRank calculates importance
├─ Comprehensive: entity weight + time + similarity
├─ Directional weight (important events have higher weight)
└─ Return Top-N + clue chain
```

**Explainability Example**:

```json
{
  "event": "Sparse Expert Layer Design of MoE Architecture",
  "score": 0.89,
  "clues": [
    {
      "stage": "recall",
      "from": "Query: Large Model Optimization",
      "to": "Entity: MoE Architecture",
      "confidence": 0.92
    },
    {
      "stage": "expand", 
      "from": "Event: MoE Architecture Advantages",
      "to": "Event: Sparse Expert Layer Design",
      "shared_entities": ["MoE Architecture", "Expert Layer"],
      "confidence": 0.85
    }
  ]
}
```

---

## 🚀 Quick Start

### Method 1: Docker Compose (Recommended)

```bash
# 1. Clone the project
git clone https://github.com/Zleap-AI/SAG.git
cd SAG

# 2. Configure environment
cp .env.example .env
# Edit .env:
#   LLM_API_KEY=sk-xxx
#   MYSQL_PASSWORD=your_password

# 3. download nltk_data (first only)
python scripts/download_nltk_data.py

# 4. Start services
docker compose up -d

# 5. Access
# Frontend: http://localhost
# API: http://localhost/api/docs
```

### Method 2: Python SDK

```python
import asyncio
from sag import SAGEngine
from sag.modules.load.config import LoadBaseConfig
from sag.modules.extract.config import ExtractBaseConfig
from sag.modules.search.config import SearchBaseConfig

async def main():
    # Initialize
    engine = SAGEngine(source_config_id="my-project")
    
    # Load documents
    await engine.load(LoadBaseConfig(
        type="path",
        origin=["./docs/article.md"],
        background="Technical Documentation"
    ))
    
    # Extract events
    await engine.extract(ExtractBaseConfig(
        parallel=True,
        background="AI Large Model Documentation"
    ))
    
    # Intelligent retrieval
    result = await engine.search(SearchBaseConfig(
        query="How to optimize large model inference speed?",
        depth=2,
        top_k=10
    ))
    
    # View results
    for event in result.events:
        print(f"[{event.score:.2f}] {event.title}")
        print(f"  {event.summary}\n")

asyncio.run(main())
```

### Method 3: Web Interface

Visit http://localhost:3000

1. **Upload Documents**: Drag and drop Markdown, PDF, HTML
2. **Automatic Processing**: System automatically loads → extracts → indexes
3. **Intelligent Search**: Enter natural language queries
4. **View Results**: Browse events, clue graphs, sources



---

## 🌐 Open Source Edition vs Full Edition

### Feature Comparison

| Feature                | Open Source Basic Edition | [Full Edition](https://zleap.ai)             |
| ---------------------- | ------------------------- | -------------------------------------------- |
| **Core Engine**        | ✅ Fully Open Source       | ✅ Same Engine                                |
| **Document Loading**   | ✅ Local Files             | ✅ Multiple Information Sources               |
| **Data Sources**       | ✅ Manual Upload           | ✅ Automatic Updates                          |
| **Content Publishing** | ❌                         | ✅ One-click Article/Report Generation        |
| **Collaboration**      | ❌ Single User             | ✅ Team + Permission Management               |
| **Advanced Features**  | ❌                         | ✅ Intelligent Recommendations + Auto Summary |
| **Cloud Service**      | ❌ Self-hosted             | ✅ Ready to Use                               |
| **Support**            | Community                 | Professional Technical Team                  |

### Why Open Source Basic Edition?

We believe:

- 🌍 **Technology Sharing**: Core algorithms should be used and improved by more people
- 🔧 **Flexible Deployment**: Enterprises can build private deployments
- 🤝 **Community-Driven**: Open source community feedback makes products better
- 💡 **Innovation Incentive**: Developers can build their own applications based on SAG

### When to Use Full Edition?

- Need automatic web tracking and information flow management
- Want to integrate more information sources
- Need team collaboration and permission management
- Hope for zero deployment, ready to use
- Need professional technical support

**Try Full Edition**: [https://zleap.ai](https://zleap.ai)



---
## 📖 Deep Learning

>This section is a brief explanation for developers and researchers interested in algorithm details.

### 🧠 Core Concept: Event & Natural Language Vector
SAG's underlying philosophy can be summarized in two sentences:

- **Event Atomization**  
  *Instead of "mechanically chunking" by character/Token length, convert documents into **semantically complete, mutually independent** "Events".*
- **Natural Language Vector**  
  *Not just encoding entire text segments into vectors, but extracting multi-dimensional entities for each Event: time, location, person, action, topic, tags…  
  They form a "**vector composed of natural language entities**".*

**Key Insights**:  
- Event is the **atomic knowledge unit**  
- Entity is the **attribute dimension of events**  
- Relationships between entities are not calculated in advance, but **dynamically calculated at query time**

### 🧮 Three-Stage Search Algorithm (Recall → Expand → Rerank)


### 1. Recall: Entity-Driven Recall

**Goal**: Starting from the query statement, find a batch of highly relevant **entities + events**.

- **Step Summary**:
  - **LLM Parses Query**: Extract structured entities (TOPIC, ACTION, PERSON…)
  - **Vector Retrieval of Entities**: Search in entity vector space  
  - **Query Events with Entities (SQL)**: Reverse lookup events through entity IDs  
  - **Event Vector Retrieval**: Query directly on Event vectors  
  - **Intersection Filtering + Weight Backpropagation**: Balance semantic similarity and entity matching



### 2. Expand: BFS-Based Multi-hop Expansion

**Goal**: Through "shared entity patterns", perform **multi-hop search** in event-entity space to find deeper related information.

- **Approach**:
  - Treat high-weight entities as current "frontier layer"  
  - Use these entities to find new events in SQL  
  - Calculate similarity and weight for new events, and backpropagate weights to new entities  
  - Only keep "newly appeared" entities, forming the next hop frontier layer  
  - Process includes **weight decay** + **deduplication**, automatically converges when no new entities

- **Characteristics**:
  - Similar to "Six Degrees of Separation": Any two events can often be connected through a few intermediate entities  
  - Depth 2 usually achieves optimal balance in **precision / recall / latency**
  
- **Entity Weight Formula**:

$$W(k_i) = \sum_{e_j \in E} \left[ W_{e2}(e_j) \times \frac{count(k_i, e_j)}{\ln(1 + step_{ij})} \right]$$

### 3. Rerank: Directional PageRank-Based Ranking

On the event subgraph obtained from Recall + Expand, SAG constructs an implicit graph and runs **weighted PageRank**:

- **Nodes**: Events `e`  
- **Directed Edges**: Shared entity relationships, edge weights determined by entity weight + frequency:

$$
W(e_i \rightarrow e_j) = \sum_{k \in (e_i \cap e_j)} W_{\text{entity}}(k) \cdot \ln(1 + \text{freq}(k, e_j))
$$

- **PageRank Iteration**:

$$
\mathrm{PR}(e_j) = \frac{1-d}{N} + d \sum_{e_i \in \mathrm{In}(e_j)} \mathrm{PR}(e_i) \cdot \frac{W(e_i \rightarrow e_j)}{\sum\limits_k W(e_i \rightarrow e_k)}
$$

- **Final Comprehensive Score** (Four-Factor Weighted):

$$
S(e) = \alpha \cdot \mathrm{PR}(e) + \beta \cdot \mathrm{Sim}(Q, e) + \gamma \cdot \mathrm{EntityScore}(e) + \delta \cdot \mathrm{TimeDecay}(e)
$$

Where typical configuration: α=0.4, β=0.3, γ=0.2, δ=0.1.

---


## 🤝 Community & Contribution

### Join Us

- 🌐 Website: [https://zleap.ai](https://zleap.ai)
- 💬 Discord: [Join Discussion](https://discord.com/invite/DRCmtBJhyN)
- 📧 Email: contact@zleap.ai
- 🐦 Twitter: [@ZleapAI](https://x.com/zleapai)

### How to Contribute

```bash
# 1. Fork and clone
git clone https://github.com/your-name/SAG.git

# 2. Create branch
git checkout -b feature/amazing-feature

# 3. Commit changes
git commit -m "feat: add amazing feature"

# 4. Push
git push origin feature/amazing-feature

# 5. Open Pull Request
```

**Commit Convention**: `feat:` | `fix:` | `docs:` | `refactor:` | `test:` | `chore:`

### Contributors Wall




---

## 🙏 Acknowledgments

- Thanks to all contributors
- Special thanks to [302.AI](https://302.ai) for computing power support

---

## 📄 License


This project is licensed under [Apache-2.0 License](LICENSE)

---

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=Zleap-AI/SAG&type=Date)](https://star-history.com/#Zleap-AI/SAG&Date)

---

<div align="center">


**Connect Information, Transform Data into Assets**

Made with ❤️ by [Zleap Team](https://zleap.ai)

</div>

---






