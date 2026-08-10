"""检索评测辅助（离线 CLI 与在线 `/search/eval-compare` 共用）。"""

from sag_api.services.eval.llm_judge import (
    JudgeVerdict,
    build_pairwise_prompt,
    judge_pairwise,
)

__all__ = ["JudgeVerdict", "build_pairwise_prompt", "judge_pairwise"]
