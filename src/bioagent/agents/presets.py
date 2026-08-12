"""The frontend-facing "preset" view of the preset-pipeline registry.

A *preset* is just a :class:`~bioagent.agents.preset_pipelines.PresetPipeline` surfaced to the user
as a selectable starting prompt: the gateway lists them for the console's picker and lets the user
edit the prompt before a run (``LabConfig.preset_prompt``). The engine — loading, dataset-aware
routing, prompt composition, the progressive-disclosure reference tool — lives in
``preset_pipelines.py``; this module only re-exports that surface under the legacy
``ResearchPreset`` / ``PRESETS`` / ``get_preset`` / ``list_presets`` names so existing gateway +
test call sites keep working.

New code should import from :mod:`bioagent.agents.preset_pipelines` directly. (Note: the atomic,
model-rewritable capability layer is a SEPARATE module, :mod:`bioagent.agents.skills`.)
"""

from __future__ import annotations

from .preset_pipelines import (  # noqa: F401  (re-exported for backward compatibility)
    PIPELINES as PRESETS,
    PresetPipeline,
    ResearchPreset,
    get_pipeline as get_preset,
    list_pipelines as list_presets,
)

__all__ = [
    "PRESETS",
    "PresetPipeline",
    "ResearchPreset",
    "get_preset",
    "list_presets",
]
