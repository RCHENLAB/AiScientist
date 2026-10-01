"""Research agents: the tool-calling harness (Scientist) and the PI→Scientist→Critic lab."""

from .research_harness import (
    HarnessConfig,
    HarnessContext,
    HarnessResult,
    HarnessTool,
    ResearchHarness,
    default_catalog,
)
from .research_lab import LabConfig, LabResult, ResearchLab, make_run_code_tool

__all__ = [
    "HarnessConfig", "HarnessContext", "HarnessResult", "HarnessTool", "ResearchHarness", "default_catalog",
    "LabConfig", "LabResult", "ResearchLab", "make_run_code_tool",
]
