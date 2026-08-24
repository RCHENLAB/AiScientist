#!/bin/bash
#SBATCH --job-name=bioagent-vllm
#SBATCH --partition=free-gpu32
#SBATCH --account=ruic20_lab
#SBATCH --gres=gpu:RTX6000:2
#SBATCH --cpus-per-task=16
#SBATCH --mem=90G
#SBATCH --time=03:00:00
#SBATCH --output=vllm27-build/serve-%j.log
set -euo pipefail
source /etc/profile.d/lmod.sh 2>/dev/null || true
module load singularity/3.11.3
BASE=/dfs3b/ruic20_lab/software/AiScientist
export HF_HOME=$BASE/hf
PORT=$(( 20000 + RANDOM % 20000 ))
mkdir -p ~/.bioagent; echo "$PORT" > ~/.bioagent/vllm.${SLURM_JOB_ID}.port; echo "$PORT" > ~/vllm27-build/dsv4.port
echo "[serve] $(date) host=$(hostname) port=$PORT"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
SPEC='{"method":"dspark","num_speculative_tokens":7,"draft_sample_method":"greedy"}'
EXTRA="${DSV4_EXTRA:-}"
singularity exec --nv -B $BASE:$BASE -B $BASE/containers/vllm-0.27.1-patches/tilelang.py:/usr/local/lib/python3.12/dist-packages/vllm/model_executor/kernels/mhc/tilelang.py --env HF_HOME=$HF_HOME --env HF_HUB_OFFLINE=1 --env NCCL_DEBUG=INFO --env NCCL_IB_DISABLE=1 --env NCCL_SOCKET_IFNAME=lo --env VLLM_SKIP_P2P_CHECK=1 --env VLLM_USE_DEEP_GEMM=0 --env VLLM_DISABLED_KERNELS=CutlassFp8BlockScaledMMKernel,FlashInferFp8DeepGEMMDynamicBlockScaledKernel,DeepGemmFp8BlockScaledMMKernel --env NCCL_CUMEM_ENABLE=0 --env NCCL_P2P_DISABLE=1 \
  $BASE/containers/vllm-0.27.1.sif \
  vllm serve MJPansa/DeepSeek-V4-Flash-0731-NVFP4 --host 0.0.0.0 --port $PORT --served-model-name MJPansa/DeepSeek-V4-Flash-0731-NVFP4 \
  --trust-remote-code --tokenizer-mode deepseek_v4 --tensor-parallel-size 2 --kv-cache-dtype fp8 --block-size 256 \
  --max-model-len 262144 --gpu-memory-utilization 0.90 --enable-auto-tool-choice --tool-call-parser deepseek_v4 \
  --reasoning-parser deepseek_v4 --speculative-config "$SPEC" --disable-custom-all-reduce $EXTRA
