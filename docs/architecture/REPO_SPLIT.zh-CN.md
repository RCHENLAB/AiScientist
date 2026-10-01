# 拆成三个仓库

本仓库如何变成 **RCHENLAB/AiScientist**（平台）、**RCHENLAB/AiScientist-tools** 和
**RCHENLAB/AiScientist-skills**，以及在 main 上的工具还在修改时如何合并这次的准备分支。
English: [REPO_SPLIT.md](REPO_SPLIT.md)。架构总览：[README.zh-CN.md](README.zh-CN.md)。

## 目标结构

| 仓库 | 包含 | 平台如何使用它 |
|---|---|---|
| RCHENLAB/AiScientist | `gateway/`、`agents/`、`reporting/`、`hpc/`、`core/`、`frontend/`、`deploy/`、`docs/`、平台测试与契约测试 | （本身） |
| RCHENLAB/AiScientist-tools | `src/bioagent/tools/`（一个工具一个文件夹，`sdk.py`、`catalog.py`、`api.py`、`_lib/`、参考数据、作业入口）、工具测试、`scripts/tool_docs.py` | Python 包 `aiscientist-tools`，固定到某个 tag；import 路径仍是 `bioagent.tools` |
| RCHENLAB/AiScientist-skills | `skills/`、`pipelines/`（现在叫 `preset_pipelines/`） | 部署时检出固定 tag；用 `BIOAGENT_SKILLS_DIR` / `BIOAGENT_PIPELINES_DIR` 指向它 |

## 契约

* **依赖只朝一个方向。** 平台 import 工具、读取 skills；工具不 import 平台的任何代码（`tests/test_tools_boundary.py`）；skills 只是文件。
* **工具契约只有一个文件 `bioagent/tools/sdk.py`**：`HarnessTool`、`ToolContext` 协议，以及向会话模型发起有界调用的 `session_chat_fn`（后端由网关注册）。
* **平台访问工具只有三扇门**：`sdk`、`catalog`（从 `TOOL.md` 发现工具）、`api`（平台需要的其他所有名字），并且只用公开名字（`tests/test_repo_boundaries.py`）。
* **新增工具不改平台。** 一个带 `TOOL.md` + `tool.py` 的文件夹；registry、HPC3 路由（`runs_on`）、聊天工具（`chat`）、分析作业分发器和 System 页面都读清单。
* **import 路径不变。** `bioagent` 变成命名空间包：平台提供 `bioagent.gateway`、`bioagent.agents` 等，AiScientist-tools 提供 `bioagent.tools`。这符合 CLAUDE.md“代码命名空间保持 bioagent”的规定，所有 `python -m bioagent.tools...` 作业入口（包括写死在镜像 runscript 里的）都不用改名。
* **跨仓库的契约测试放在平台仓库**（它同时依赖另外两个）：`tests/test_tool_contract.py`（平台逻辑依赖的工具名、skills/pipelines 提到的工具名都存在）、`tests/test_declared_params.py`（pipeline 文档写的默认值与代码一致）、`tests/test_skills_library.py`（skills 与 pipelines 格式正确）。

## 当前状态

第 0 阶段（先在本仓库里把边界做实）已在分支 `claude/aiscientist-architecture-refactor-b0fcaa` 完成，行为不变：

1. `tools/sdk.py` 和 `tools/api.py`；工具不再 import 平台。
2. 平台代码移出 `tools/`：`reporting/`（报告渲染、结果包、参考文献、视觉审阅）和 `hpc/shell.py`。
3. 一个工具一个文件夹，由 `scripts/refactor/split_tools.py` 生成；20 份带文档的 `TOOL.md` 清单；`agents/registry.py` 和分析作业分发器都读清单。
4. 边界、清单、契约和 skills 测试；`_pack_bioagent_source` 能合并命名空间包的多个部分。

2026-09-30 用 `scripts/refactor/extract_repos.py` 在全新克隆上做了预演：

| 仓库 | 提交数 | 文件数 | 测试 |
|---|---|---|---|
| AiScientist-tools，单独运行 | 169 | 113 | 346 通过 |
| AiScientist，工具在路径上、skills 来自独立检出 | 766 | 461 | 1670 通过，3 跳过 |
| AiScientist-skills | 71 | 52 | （由平台检查） |

两个测试数相加等于单仓库的全部测试。历史得以保留（例如 `git log --follow src/bioagent/tools/literature_search/tool.py` 能追溯到该工具的第一次提交）。

## main 上工具还在改时如何合并本分支

拆分是由程序生成的，所以 main 上的工具修复不需要手工搬运。2026-09-30 做过演练：main 上真实的新提交，加上模拟的
`run_de` 内的修复、共享函数 `_slug` 的修复、已搬走的 `literature_search.py` 的修复，以及一个按旧路径写的新测试——
全部落到正确的文件里，测试全部通过（2022 个）。

```bash
git checkout claude/aiscientist-architecture-refactor-b0fcaa
git merge main
# 只会在本分支拆分过的文件（scrna_pack.py、scrna_advanced.py、phenotype_dx.py）上
# 出现 modify/delete（"DU"）冲突，保持删除：
git status --short | awk '$1 == "DU" {print $2}' | xargs git rm
# 用 main 的新版本重新生成拆分后的模块，并修正合并带进来的旧 import：
python scripts/refactor/split_tools.py --regenerate-from main
python scripts/tool_docs.py
python -m pytest
git commit
```

各部分如何处理：

* **整文件移动的文件**（如 `literature_search.py` → `literature_search/tool.py`）：合并时 git 的改名检测会自动把 main 的修改套上去。
* **三个被拆分的文件**：`--regenerate-from main` 用 main 的文本重新切分，所以 `run_de` 里的修复会落到 `run_de/tool.py`，共享函数的修复会落到 `_lib/scrna.py`。分支在生成代码之上需要的修改写在脚本里（`GENERATED_EDITS`）。
* **main 上 import 旧模块的新代码**（比如新测试写了 `from bioagent.tools import scrna_pack`）：自动改指向定义它的新模块。
* **main 上在 `tools/` 下新增的模块或工具**：脚本会停下并指出它；把它加进 `split_tools.py` 的计划（整体移动、拆分或 `STAY`），新工具文件夹补一份 `TOOL.md`（没有的话 `tests/test_tool_manifests.py` 会失败）。

## 正式拆分

随时可以用 `python scripts/refactor/extract_repos.py --out <空目录>` 预演；它不会改动本地检出，也不会推送任何远程。正式拆分按同样步骤进行。

### 第 1 阶段：AiScientist-skills

1. 新建空仓库 RCHENLAB/AiScientist-skills。
2. 由 `extract_repos.py` 生成（`git filter-repo --path skills/ --path preset_pipelines/ --path-rename preset_pipelines/:pipelines/`，外加一个 README 提交）。推送并打 tag `v0.1.0`。
3. 在平台仓库：删除 `skills/` 和 `preset_pipelines/`；在线上 `.env` 里把 `BIOAGENT_SKILLS_DIR`、`BIOAGENT_PIPELINES_DIR` 指向该 tag 的检出；让 `scripts/sync_deploy.sh` 检出固定 tag。归纳生成的 skill 留在运行时状态目录，不进 git。
4. skills 仓库的 CI 跑 skills 检查（`tests/test_skills_library.py` 读的是配置的目录，所以平台 CI 也会对固定 tag 跑一遍）。

### 第 2 阶段：AiScientist-tools

1. 新建 RCHENLAB/AiScientist-tools；`extract_repos.py` 生成它，并带上 `pyproject.toml`（包名 `aiscientist-tools`，命名空间打包）和 README。推送并打 tag `v0.1.0`。
2. 在平台仓库：删除 `src/bioagent/tools/`、已搬走的测试和 `src/bioagent/__init__.py`；依赖 `aiscientist-tools @ git+https://github.com/RCHENLAB/AiScientist-tools@v0.1.0`；平台自身的打包改为支持命名空间（setuptools 在 `src` 下用 `find_namespace:`）。
3. 先卸载本仓库任何旧的可编辑安装。预演中发现：仍带 `bioagent/__init__.py` 的旧安装是普通包，而普通包优先于命名空间包的各部分，会悄悄遮住新仓库里的 `bioagent.tools`。
4. 部署：网关已经会把 `bioagent` 的所有部分同步到 HPC3（`_pack_bioagent_source`），作业镜像无需重建就能看到工具。如果工具安装在同步目录以外的位置，`BIOAGENT_GENESETS_DIR` 要指向 `.gmt` 文件目录。

### 第 3 阶段：在 RCHENLAB 下开发

* **已定（2026-09-30）：** RCHENLAB/AiScientist 保持公开，除凭据（数据库账号密码、API key、token、私钥）外全部公开。在开发正式搬过去之前，用 `scripts/publish_public_mirror.py` 以快照方式更新，脚本发现任何像凭据的内容就拒绝发布。对全部历史中每一处文本改动（1401 个提交、所有分支）的扫描没有发现任何提交过的凭据，所以保留历史切出的 tools 和 skills 仓库也不会带出凭据。
* 在公开仓库里开发之后，每次 push 都是公开的：给 RCHENLAB 的仓库打开 GitHub secret scanning 和 push protection（公开仓库免费），凭据照旧只放在部署的 `.env` 和 `BIOAGENT_STATE_DIR` 里。
* 每个仓库各自的 CI；按工作线设置 CODEOWNERS；平台 README 链接另外两个；迁移完成后旧仓库冻结为只读。

## 相关文件

| 路径 | 作用 |
|---|---|
| `scripts/refactor/split_tools.py` | `--plan`、`--apply`（一工具一文件夹的切分）、`--regenerate-from REF`（合并之后用） |
| `scripts/refactor/extract_repos.py` | 从全新克隆中切出三个仓库（预演或正式） |
| `scripts/tool_docs.py` | 重新生成每个 `TOOL.md` 的生成区和工具索引 |
| `scripts/publish_public_mirror.py` | 生成公开仓库 RCHENLAB/AiScientist 的快照，并检查凭据 |
| `src/bioagent/tools/catalog.py` | 从 `TOOL.md` 发现工具 |
| `src/bioagent/tools/sdk.py`、`src/bioagent/tools/api.py` | 契约与平台的入口 |
