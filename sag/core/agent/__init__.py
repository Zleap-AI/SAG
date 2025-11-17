"""
Agent 模块

基于 JSON 系统提示词的智能数据处理 Agent 系统
"""

from sag.core.agent.base import BaseAgent
from sag.core.agent.builder import Builder
from sag.core.agent.researcher import ResearcherAgent
from sag.core.agent.summarizer import SummarizerAgent

__all__ = [
    "BaseAgent",
    "Builder",
    "ResearcherAgent",
    "SummarizerAgent",
]

__version__ = "2.0.0"
