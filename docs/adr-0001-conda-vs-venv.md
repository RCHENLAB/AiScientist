# ADR-0001: 环境管理保持 venv + pip，Conda 暂不迁移

- 日期：2026-06-12
- 状态：已决定（可随触发条件重新评估）

## 背景

导师建议考虑用 Conda 替代 Python venv，理由是 Conda 在部分场景支持 R、便于管理环境；
也提到必要时可拉独立 container。问题在于：HPC3 上 container 需走 Singularity，较麻烦。

## 现状

- 核心用 **venv + pip + editable install**（`pyproject.toml` extras：`gateway` / `biomni` / `analysis`）。
- 部署 `deploy.sh` 在服务器建 venv；系统级二进制（pandoc/texlive/graphviz/d2）走 apt。
- 仓库无 Dockerfile/Singularity；HPC3 通过 gateway **SSH 投递作业**，非容器化执行。
- 当前唯一 Conda 用例是 Kosmos baseline（对照评测），且本分支正移除 Kosmos CLI。
- **R 目前完全未使用**；分析线为纯 Python（scanpy/gseapy）。

## R 工具的 Python 替代

| R 工具 | Python 替代 | 状态 |
|---|---|---|
| Seurat | scanpy | 已用，完全对等 |
| DESeq2 | PyDESeq2 | 忠实移植，一行 pip |
| fgsea/GSEA | gseapy | 已用 |
| Harmony | harmonypy | 一行 pip |
| Monocle/trajectory | scanpy(PAGA)/scVelo/CellRank | 一行 pip |
| limma | 无直接移植（唯一明显短板） | 当前用不到 |

## 决定

**保持 venv + pip，暂不迁移 Conda，也不上 container。**

当前单细胞 + 富集路线 Python 生态无功能缺口，venv 更轻、CI 快、HPC3 上无需 root。

## 重新评估的触发条件（满足任一即考虑）

1. 需要只有 R 实现的工具（如 limma）；
2. 某 Python 包在 HPC3 上 pip 装不上（需编译/缺系统库且无 root）——这是 Conda 在 HPC3 不可替代之处。

## 迁移预案（届时执行，约半天）

- 用 **micromamba**（单二进制、用户态、对 `deploy.sh` 侵入最小），非完整 Conda。
- Conda 只管「pip 装不动的那层」（如 r-base），pip 继续管 Python 包：
  `environment.yml` 里 `pip: ["-e .[gateway,analysis]"]` 复用现有 extras。
- `deploy.sh` 仅改环境创建一步（`python -m venv` → `micromamba create -f environment.yml`），
  extras 定义与业务代码不动。

现有 `pyproject.toml` extras 设计已是迁移友好，故现在不切无沉没成本，将来切代价也低。
