"""AiScientist — a local-first, privacy-preserving single-cell RNA-seq research agent.

The product is the HPC3 web console (``aiscientist.gateway``): SSH → UCI HPC3 → a
GPU-served Qwen3.8 (vLLM), driving a PI→Scientist→Critic research lab over a curated
single-cell analysis toolset.
"""

from .core.config import apply_brand_env_aliases as _apply_brand_env_aliases

# The env vars are AISCIENTIST_*; a deployment may still set the legacy BIOAGENT_* names (the prod
# .env, systemd's EnvironmentFile, an sbatch job queued before a rename). Mirror the two prefixes
# before any submodule is imported, so a module-level read sees the value whichever name was set.
_apply_brand_env_aliases()

__all__: list[str] = []
