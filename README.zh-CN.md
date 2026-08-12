# AiScientist 中文说明

这是一个 local-first 的视觉/眼科生物学多智能体生信分析原型，对应 proposal 中的本地 AiScientist 方向。

当前原型刻意保持小而可控：

- 默认不需要外部 API key
- 默认不做真实 Slurm 提交
- 不上传生物医学数据
- 有确定性的 agent handoff 和 workflow memory
- 生成 dry-run `sbatch` 脚本，后续用于接入 UCI HPC3/RCIC

## 本地 Web Chat 原型

第一版 lab-facing web prototype 是 chat-first workspace，由一个标准库 Python 后端提供 API 和静态前端。它支持多轮 AiScientist session、HPC 授权提示、真实本地 workflow 执行、artifact 列表，以及下载生成的 `final_report.md`。

从仓库根目录启动：

```bash
PYTHONPATH=src python3 -m bioagent.web_server --port 8000
```

然后访问 `http://localhost:8000/frontend/`。

前端不是 Slurm client。浏览器不应该保存 SSH 私钥，也不应该绕过 UCI Duo/account policy。本地 v1 的 SSH/HPC 授权弹窗是 future gateway 占位，不收集密码、Duo code 或 private key。

HPC3 连接弹窗遵循 RCIC 登录边界：

- HPC3 host: `hpc3.rcic.uci.edu`
- 直接交互访问使用 SSH
- password-based login 需要 Duo
- 用户需要在 UCI campus network 或 UCI VPN
- 用户生成的 SSH key 必须有非空 passphrase，且不能共享

参考：`https://rcic.uci.edu/account/login.html`、
`https://rcic.uci.edu/guides/beginner.html` 和
`https://rcic.uci.edu/slurm/jobs.html`。

后续接 Ollama/Qwen 时，暴露 OpenAI-compatible endpoint 并设置：

```bash
BIOAGENT_LLM_BASE_URL=http://localhost:11434/v1
BIOAGENT_LLM_MODEL=Qwen/Qwen3.6-35B-A3B
BIOAGENT_LLM_API_KEY=local-dev-key
```

未配置本地 endpoint 时，web run 会明确显示 offline mode，但仍会生成本地 workflow artifacts 和报告。

## 运行

```bash
PYTHONPATH=src python3 -m bioagent "Run retina single-cell RNA-seq QC and differential expression planning" --workspace runs/demo
```

CLI 会自动读取 `.env`。OpenRouter 原型测试可使用：

```bash
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=qwen/qwen3.6-35b-a3b
```

后续 lab server 或 Slurm job 上的本地 vLLM / SGLang 可以使用这种配置形态：

```bash
BIOAGENT_LLM_BASE_URL=http://localhost:8000/v1
BIOAGENT_LLM_MODEL=Qwen/Qwen3.6-35B-A3B
BIOAGENT_LLM_API_KEY=local-dev-key
```

## Lab Server 与 HPC 队列策略

proposal 将基础设施分成两层：

- Lab server：chatbot UI、coordinator LLM、vector memory、skill library、轻量 preflight。
- UCI HPC3/RCIC：通过 Slurm 执行高算力 CPU/GPU 分析，包括大规模 single-cell、spatial、multi-omics 和模型密集型 workflow。

当 UCI GPU 队列较长时，AiScientist 不应该静默把高算力 GPU job 移到 lab server。相反，每个 Slurm dry-run 都会写出：

- `artifacts/slurm_job.sh`
- `artifacts/hpc_queue_fallback_plan.md`
- `artifacts/hpc_queue_fallback_plan.json`

队列 fallback plan 会提醒研究人员任务可能在 GPU 队列中等待，并提供安全选项：继续等待并保留 checkpoint、只在 lab server 跑轻量 preflight、提交较小的经审核 diagnostic GPU job、经人工 review 后降低资源请求，或暂停并稍后恢复。

离线 dry run：

```bash
PYTHONPATH=src python3 -m bioagent "Run retina single-cell RNA-seq QC and differential expression planning" --workspace runs/demo --no-llm
```

该命令会打印简短 research answer，并写出：

- `runs/demo/memory.jsonl`
- `runs/demo/artifacts/slurm_job.sh`
- `runs/demo/artifacts/final_report.md`

## 测试

```bash
python3 -m pytest
```

## Harness

Harness 用来检查 pipeline 逻辑，不代表真实 Slurm 或真实生物学分析已经执行。

```bash
PYTHONPATH=src python3 -m bioagent.harness --workspace runs/harness-openrouter
```

离线模式：

```bash
PYTHONPATH=src python3 -m bioagent.harness --workspace runs/harness-offline --no-llm
```

规则文档：`docs/archive/harness_rules.md`。

## Kosmos Kernel Safety Smoke

AiScientist 当前把 Kosmos 作为计划中的 autonomous research-loop kernel，但在前面包了一层本地 permission 和 data-boundary harness。这个 smoke test 不调用 Kosmos、OpenRouter、literature APIs 或 Slurm，只验证高风险能力默认被阻断。

```bash
PYTHONPATH=src python3 -m bioagent.kosmos_smoke --workspace runs/kosmos-kernel-smoke
```

默认 smoke 策略：

- literature/network tools 被阻断
- 真实 `sbatch` 提交被阻断
- raw expression rows 不能进入 prompt
- generated code execution 需要 sandbox

规则文档：`docs/archive/kosmos_kernel_guardrails.md`。

## Iterative Real-Dataset Harness

用于让同一个数据集经过多轮本地 AiScientist，并自动评估 artifacts、privacy guardrails、Slurm dry-run 状态、QC readiness 和 claim control。

```bash
PYTHONPATH=src python3 -m bioagent.iterative_harness \
  --dataset runs/comparison-report/public_data/pbmc3k_raw.h5ad \
  --workspace runs/real-pbmc3k-iterative \
  --rounds 3
```

这是 iterative harness，还不是 autonomous code self-repair。它可以根据评估结果调整下一轮问题，但当前不会重写源码，也不会启用真实 Slurm submit。未来 autonomous repair 应该经过 proposed diff、测试、artifact 对比和人工确认，安全后允许替换旧版本，而不是永远累积新版本。

## Checkpointed Autonomous Loop

该原型支持 budget-aware autonomous loop，并带可恢复 checkpoints。它会在每轮和配置的时间间隔写 checkpoint；启用 OpenRouter 时会记录 token usage；即使中断，也可以从已有 checkpoints 重建最终报告。

离线 smoke：

```bash
PYTHONPATH=src python3 -m bioagent.autonomous_loop \
  --dataset examples/datasets/retina_scrna_toy.csv \
  --workspace runs/autonomous-smoke \
  --run-mode smoke \
  --checkpoint-minutes 0.02 \
  --max-rounds 2
```

OpenRouter autonomous research demo：

```bash
PYTHONPATH=src python3 -m bioagent.autonomous_loop \
  --dataset runs/comparison-report/public_data/pbmc3k_raw.h5ad \
  --workspace runs/autonomous-research-pbmc3k \
  --run-mode research \
  --use-openrouter \
  --checkpoint-minutes 15 \
  --time-abnormal-minutes 30 \
  --soft-token-budget 120000 \
  --hard-token-budget 200000 \
  --min-rounds 6 \
  --min-total-tokens-before-stop 15000 \
  --max-rounds 8
```

Research mode 不是 smoke test。它会生成分阶段 research deliverables：dataset/QC readiness、sanitized PubMed grounding、generated summary-code execution、marker interpretation planning、confounder critique、hypothesis generation、validation experiment design、claim-boundary handoff synthesis。它仍然阻断 raw data upload、真实 Slurm submit、generated-code network access 和 validated biological claims。

Research mode 中的 literature search 只允许使用 sanitized public query。query builder 会剥离本地路径，且不会把 raw expression rows、sample identifiers 或 private metadata 发送到 PubMed/OpenRouter。强制关闭 literature search：

```bash
PYTHONPATH=src python3 -m bioagent.autonomous_loop \
  --dataset runs/comparison-report/public_data/pbmc3k_raw.h5ad \
  --workspace runs/autonomous-research-pbmc3k \
  --run-mode research \
  --use-openrouter \
  --disable-literature-network
```

Generated code execution 当前只运行在派生 JSON artifacts 上，例如 `dataset_results.json`、`single_cell_qc_execution.json`、`de_marker_execution.json` 和 `literature_grounding.json`。它不会 mount 或读取原始 `.h5ad` / `.csv` 数据集，也不允许网络访问。

从已有 checkpoints 生成最终报告，不调用模型：

```bash
PYTHONPATH=src python3 -m bioagent.autonomous_loop \
  --workspace runs/autonomous-openrouter \
  --finalize-only
```

## CI

GitHub Actions 只运行离线检查：

- unit tests
- offline AiScientist harness
- Kosmos kernel safety smoke
- toy retina dataset iterative harness
- checkpointed autonomous loop smoke

CI 故意不依赖 `.env`、OpenRouter、live Kosmos、Docker 或 Slurm。

## Agent 开发管理 Dashboard

协作开发时用这些文件管理多 agent 架构：

- `docs/agent_registry.yaml`：每个 agent/component 的 source-of-truth registry，包括 owner、状态、证据和下一步。
- `docs/archive/agent_dashboard.md`：给协作者看的状态 dashboard。
- `docs/agent_architecture.mmd`：Mermaid 架构图，颜色和 Figma 图例保持一致。

Multica 更适合管理开发任务和协作者；repo dashboard 负责记录每个 AiScientist agent 到底是什么、哪些已经实现、哪些还在计划中。

## Kosmos Parity and Gap Evaluation

这些命令用于评估 AiScientist 是否达到实验室使用 Kosmos/Edison-style workflow 所需的能力水平。报告是 parity/gap report，不是胜负评测。Kosmos 失败会被标记为 setup、timeout、local-data integration 或 research-quality evidence 问题。

```bash
PYTHONPATH=src python3 -m bioagent.comparison --scenario toy-retina --workspace runs/parity-local-data --timeout-seconds 30
```

Kosmos-suited autonomous research-loop benchmark：

```bash
PYTHONPATH=src python3 -m bioagent.comparison --scenario autonomous-retina-loop --workspace runs/parity-autonomous-loop --timeout-seconds 120
```

AiScientist + Kosmos bridge integrated scenario：

```bash
PYTHONPATH=src python3 -m bioagent.comparison --scenario integrated-kosmos-bridge --workspace runs/parity-integrated-bridge --timeout-seconds 120
```

每次运行会写出 `parity_report.md`、`parity_report.json`，并生成兼容别名 `performance_report.md/json`。

## Dataset Smoke Runs

原型可以在本地运行 toy CSV 数据集。数据内容留在本地；LLM 只收到高层 workflow context。

```bash
PYTHONPATH=src python3 -m bioagent "Run retina single-cell RNA-seq QC and differential expression planning" --dataset examples/datasets/retina_scrna_toy.csv --workspace runs/datasets/retina_scrna
```

```bash
PYTHONPATH=src python3 -m bioagent "Plan spatial transcriptomics pathway interpretation for RPE disease tissue" --dataset examples/datasets/rpe_spatial_toy.csv --workspace runs/datasets/rpe_spatial
```

## 当前形态

当前原型是本地 orchestration skeleton。长期 repo plan 见 `docs/archive/project_plan.md`，模块边界见 `docs/repository_boundaries.md`。

当前内部 package layout：

```text
src/bioagent/
  core/          shared models and config
  agents/        agent roles and handoff logic
  workflows/     end-to-end workflow ordering
  tools/         deterministic local analysis tools
  hpc/           Slurm and cluster boundaries
  memory/        workflow memory and provenance
  providers/     OpenRouter/local OpenAI-compatible LLM clients
  integrations/  Kosmos/Edison/future framework adapters
  eval/          harness and regression checks
```

当前 workflow：

1. `CoordinatorAgent` 将自然语言请求转为结构化 plan。
2. `LiteratureAgent` 记录 biological context 和 evidence needs。
3. `DataAgent` 选择分析 workflow，例如 scRNA-seq QC 或 differential expression。
4. `LocalAnalysisAgent` 可选地运行本地 CSV 或 H5AD smoke analysis。
5. `SingleCellQCExecutionAgent` 计算受限 QC execution result。
6. `DifferentialExpressionExecutionAgent` 计算 lightweight marker/effect screen。
7. `HPCAgent` 准备 dry-run Slurm job script。
8. `ValidationAgent` 检查 reproducibility、privacy 和 execution readiness。
9. `ResearchEvaluationAgent` 评估 evidence strength 和 claim status。
10. `ReporterAgent` 写出面向用户的 answer，并持久化 artifacts。

当前代码已经包含 `KosmosAdapter` 和 `KosmosKernelAdapter`，用于 tool selection、permission modeling 和 data-boundary planning。未来缺少的是真实 `KosmosRuntimeClient`，以及 `EdisonAdapter` 扩展和 `SlurmAdapter.submit`。
