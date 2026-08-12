"""Real HPC3 SSH + vLLM gateway for the AiScientist web console.

This package turns the previously stubbed "HPC connection" placeholder into a
working gateway that:

- opens a real SSH session to UCI HPC3 (paramiko, password+Duo or SSH key),
- ensures a GPU allocation through Slurm and monitors its health,
- verifies the vLLM Singularity image is staged on shared DFS,
- tunnels to the vLLM /v1 endpoint and drives Qwen3 for research chat.

Every failure is surfaced with its full cause and traceback (see ``errors``)
so the UI never hides why something went wrong.

A ``mock`` executor simulates the whole remote host so the flow and UI can be
demonstrated and tested without real HPC3 access.
"""

from .errors import GatewayError, error_detail

__all__ = ["GatewayError", "error_detail"]
