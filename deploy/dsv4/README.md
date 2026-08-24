# DeepSeek-V4-Flash on HPC3 — status 2026-08-19

**Verdict: blocked on gpu32 (RTX PRO 6000, sm_120).** The checkpoint's FP8 layers use DeepSeek's
UE8M0 scale format; only DeepGEMM implements it, and DeepGEMM's SF transform asserts
`Unknown SF transformation` on sm_120 (Cutlass c3x: no sm_120 dispatch; Triton: `KeyError
'float8_e8m0fnu'`). Reproduced on vLLM 0.27.1 and the 2026-08-19 nightly, with both
`MJPansa/DeepSeek-V4-Flash-0731-NVFP4` and `nvidia/DeepSeek-V4-Flash-NVFP4`.

What DOES work (and is staged for the day DeepGEMM gains sm_120 or an AWQ requant appears):
- `containers/vllm-0.27.1.sif`, `containers/vllm-nightly.sif` (built on HPC3, Sylabs-free path);
- both weight sets under the shared `hf/hub` (127 GB + 126 GB);
- `serve_dsv4_nvfp4.sbatch` — the serve job (NCCL fixes included: `NCCL_SOCKET_IFNAME=lo`,
  `NCCL_IB_DISABLE=1`, P2P off; gateway-compatible job name + port file);
- `tilelang_sm120_patch.py` — bind over
  `/usr/local/lib/python3.12/dist-packages/vllm/model_executor/kernels/mhc/tilelang.py`
  to fall back off DeepGEMM for the mHC hyper-connection GEMM (staged at
  `containers/vllm-0.27.1-patches/tilelang.py` on dfs3b).

Working alternative (verified full pipeline): `BIOAGENT_LAB_LLM_BASE_URL=https://openrouter.ai/api/v1`
+ `BIOAGENT_LAB_LLM_MODEL=deepseek/deepseek-v4-flash-0731` routes PI/Critic/writer to DSV4 while the
session Qwen3.6 keeps the Scientist loop. Cap DSV4's reasoning effort and give ≥32k output.
Quota fact: free-gpu32 allows 4 concurrent GPUs for the WHOLE ruic20_lab account.

**GPU placement policy (Yijun, 2026-08-19): Qwen user sessions go to the regular `gpu` partition on
A100 (billed, `ruic20_lab_gpu`); the RTX6000/gpu32 nodes are reserved for the big-PI model server
only.** Ops line for prod `.env`:
`BIOAGENT_GPU_CANDIDATES="gpu,gpu:A100:1,ruic20_lab_gpu"` (drop the free-gpu32 candidate for sessions).
