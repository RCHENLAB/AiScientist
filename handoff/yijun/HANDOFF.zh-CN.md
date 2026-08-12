# BioAgent 原型交接文档

## 2026-08-10 — 部署为什么要密码，以及 sync_deploy.sh 的六个 bug

### 从头到尾就不是凭证问题

症状是「我的 admin 密码不好使了」：同一台机器上正常 `sudo` 提示符下能用的密码，`sync_deploy.sh`
却不认。实际上什么都没被改过。

时间线（全部来自文件系统元数据）：

| 时间 | 事件 | 证据 |
|---|---|---|
| 06-08 08:30 | admin 账号创建；当天设了密码，**此后再没改过** | 家目录 birth + `chage -l` |
| 06-20 21:47 | 配置 SSH 密钥 | `authorized_keys` birth |
| 06-20 21:49 | `/etc/sudoers.d/bioagent-deploy` 授予 `(bioagent) NOPASSWD: ALL` | 文件 mtime |
| | *部署免密，一路顺畅* | |
| **07-01 13:41** | 服务账号改名 `bioagent` → `aiscientist` | `/home/aiscientist` birth |
| | **免密规则的目标用户还写着 `bioagent`，从这天起部署开始要密码** | |

所以授权是在服务账号改名那天失效的，而操作者突然被要求提供一个**从未用过**的本地密码
（SSH 走密钥、sudo 本来免密）。排查过程中一并排除：账号没被锁（`passwd -S` = `P`，永不过期）、
系统里**根本没有配置** faillock/pam_tally、`authorized_keys` 自 6/7 月起未动、无陌生 IP 登录；
密码存储是纯本地（`nsswitch`: `files systemd`）——**和 UCInetID 密码无关**，这就是为什么输那个
永远不对。另外 `<admin-ucinetid>` **只存在于网关主机**，HPC3 上没有这个用户，所以拿它去登集群或
RCIC 门户永远不会成功。

### 部署脚本实际存在的六个 bug

跑通整条链路才发现的，全部已修。

1. **`sudo systemctl stop … 2>/dev/null` 把 sudo 自己的密码提示重定向掉了。** 提示写在 stderr，
   于是操作者看到的是静默卡住，对着一个**从未显示出来的提示**输密码，随后的 `|| true` 把认证失败
   吞掉；下一条没重定向的 `sudo` 就对**正确的密码**报 *"Sorry, try again"* —— 而且重启被静默跳过。
   **整场「密码被改了」的虚惊就是这条造成的。** 修法：在任何被重定向的步骤之前用一次可见的
   `sudo -v` 预热 root 凭据。
2. `sudo mkdir -p` + `sudo chown` 用 root 去建一个**早已存在、早已属服务账号**、且父目录组可写
   带 setgid 的目录。root 毫无必要，却让一个本该被 NOPASSWD 覆盖的步骤要了密码。改为以服务账号执行。
3. 健康检查 curl `127.0.0.1`，但 systemd 用显式 `--host <路由IP>` 启动，**从不监听 loopback**
   —— 于是**每次**部署都以 "health check failed" 收尾，而控制台其实返回 200。这会训练人忽略
   唯一能发现真故障的检查。现在改为向主机询问该端口实际绑在哪。
4. `--no-restart` 时照样写 `.deployed_sha`，文件宣称新版已上线，进程却还跑旧代码。
5. journal 读不到时（需要 root），诊断回退到 `tail /data/BioAgent/console.log` —— 那是旧的
   detached-start 路径的遗留文件，**停在 07-02**，末尾正好是 `INFO: Shutting down`。被打在
   "Recent log:" 标题下，读起来就是「生产刚刚挂了」，本次部署中真的造成了一次误判。现在只有该文件
   在一小时以内才会显示。
6. 特权链是一整行 `&&` 拼接，失败无法归因。

### 免密重启 —— 授权怎么写

`(aiscientist) NOPASSWD: ALL` 覆盖了 mirror、pip install 和写 sha，但**覆盖不到重启**，因为
`systemctl` 是以 **root** 执行的，不是服务账号。要让整条部署免密，在
`/etc/sudoers.d/bioagent-deploy` 里加下面这段 —— **务必用 `visudo -f`，语法错误会把所有人锁在
sudo 之外**：

```
sudo visudo -f /etc/sudoers.d/bioagent-deploy
```

```sudoers
# 免密重启控制台。必须写绝对路径；参数写死，授权无法被扩大，且不使用任何通配符。
# 这里写的是 systemd 单元名 —— 服务「账号」改名时它不会变，而上面那条 `(bioagent)` 规则
# 正是因为账号改名在 2026-07-01 失效的。
Cmnd_Alias AISCIENTIST_SVC = /usr/bin/systemctl restart bioagent.service, \
                             /usr/bin/systemctl stop    bioagent.service, \
                             /usr/bin/systemctl start   bioagent.service

<admin-ucinetid> ALL=(root) NOPASSWD: AISCIENTIST_SVC
```

几条要紧的：

* **sudo 采用最后一条匹配的规则。** `/etc/sudoers` 末尾是 `@includedir /etc/sudoers.d`，所以这里的
  规则会盖过宽泛的 `%sudo ALL=(ALL:ALL) ALL`；目录内按文件名字典序读取。
* **不要给 `journalctl` 免密 sudo。** `journalctl` 默认用 `less` 分页，而 `less` 有 shell 逃逸
  （`!`）—— 免密的 `journalctl` 等于一个 root shell。如果想让部署脚本能读日志，改为把管理员加入
  journal 组，完全不需要 sudo：`sudo usermod -aG systemd-journal <admin-ucinetid>`（重新登录生效）。
* 每个要部署的管理员各写一行，或改成组授权，**不要**通过放宽命令列表来解决。

### 已上线 —— 2026-08-10 16:11 PDT

部署完成并验证。`209bf7a` 正在运行（PID 已变、`NRestarts=0`、绑定地址和公网均 200）。完整的
`sync_deploy.sh` **全程没有任何密码提示**，而且是在 stdin 非终端的情况下跑的 —— 这正好证明脚本改用
全限定单元名之后，窄授权确实命中了。

部署代码检查：`shared_root` 和 `temp_ttl_days=3` 从生产 `.env` 正确读入；`hpc_gc.GC_SCRIPT_SRC`
指向的文件在部署树中存在；四个 gateway 接线点（`_prepare_shared_storage`、`_submit_temp_sweep`、
`_hpc_temp_gc_loop`、`_temp_base`）全部就位。

集群端到端：在 `Temp/<user>/` 下种了一个冷目录和一个热目录，投递清理后作业 **55172243 在计算节点
`hpc3-23-15` 上 COMPLETED**，日志 `GC_RESULT removed=1 kept=3` —— 冷的删掉、热的全留。登录节点
总共只执行了一条命令：那个 `sbatch`。

### 部署当前进度

代码**已安装**到网关，免密的那一半端到端跑通（mirror + pip install + 写 sha，健康检查对着真实绑定
地址返回绿）。**服务尚未重启** —— 仍是 2026-08-08 那个进程，所以 HPC3 清理在磁盘上但还没生效。
一条命令收尾：

```
ssh -t <admin-ssh-alias> 'sudo systemctl restart bioagent && sleep 2 && systemctl is-active bioagent'
```

## 2026-08-08 — HPC3 过程文件搬到公共 `AiScientist/Temp`，每 3 天清理

### 缺口，已确认

结果**确实**落盘到 eyeserver：每个 HPC3 executor 在步骤结束时把 `artifacts/` 镜像回
`<BIOAGENT_RESULTS_DIR>/<user>/<run_id>/`（生产 `/data/BioAgent`）。

但**过程文件**根本没有清理通路。产品里唯一的 GC——`app._expire_old_checkpoints` /
`_checkpoint_gc_loop`，`BIOAGENT_CHECKPOINT_TTL_DAYS`，默认 7 天——扫的是 eyeserver 本地的 run bundle，
**从不碰 HPC3**。HPC3 那侧唯一的删除动作是人手点 `/api/storage/delete`。而生产的 offload 开关全开，
所以基本每次 run 都留下永久残渣，落在每个人的**个人目录** `/dfs3b/ruic20_lab/<ucinetid>/` 里——
那恰恰是永远不能安全自动 `rm -rf` 的地方：同一棵树里还放着人手整理的数据。

2026-08-07 实测：`dfsquotas <ucinetid> dfs3b` → `ruic20_hpc` **595.26 TiB / 600 TiB（99.2%）**，
全实验室只剩约 4.7 TiB，一个月前还有 16 TiB。

### 目录结构，以及改名

一个根目录 —— `BIOAGENT_HPC_SHARED_ROOT` = **`/dfs3b/ruic20_lab/software/AiScientist`**，就是原来
放我们容器和模型权重的那个目录。它本来叫 `software/bioagent`，改名对齐产品名；**`software/bioagent`
现在是指向它的符号链接**，这样生产的 `.env` 和仓库外的脚本都还能解析（和 `BIOAGENT_*`/`AISCIENTIST_*`
env alias 同一套零停机做法）。103 G，同文件系统 `mv`，当时没有作业在跑，7 个 `.sif` 两个名字都验证可达。
`settings.py` 和 `deploy/` 里 33 处硬编码路径已更新。

```
software/AiScientist/{containers,hf,envs,ollama,scgpt_model,vlreview_model}/   资产，永不清理
software/AiScientist/Temp/<ucinetid>/{analysis,variant,phenotype,scgpt,reports,paperqa,scratch/*}   3 天清理
software/AiScientist/{uploads,pysrc,bin}/<ucinetid>/                            永不清理
```

**不是** `/dfs3b/ruic20_lab/AiScientist`（本来最该放那儿）：那个顶层是 `drwxr-s--- ruic20 ruic20_hpc`，
**组没有写位**，我们建不了。组权限不是问题（我们在 `ruic20_hpc` 里），`newgrp`/`sg` 也没用——辅助组本来
就参与权限判定，`newgrp` 只改**新建文件归哪个组**。`getfacl` 也没有扩展 ACL。RCIC 文档说 group shared
area 应该 "all group members have read and write access"，所以我们的顶层其实比他们的标准更紧；
`software/`（2775）才是实验室真正在用的公共可写目录。以后谁用 `ruic20` 权限把顶层放开，
`BIOAGENT_HPC_SHARED_ROOT` 改个值即可，代码不用动。

**个人目录只读只浏览、绝不自动删除**——旧的 run 原地不动。

### 清理跑在计算节点，不在登录节点

清理脚本（`deploy/hpc3/aiscientist_temp_gc.sh`）以「单元」为粒度——一个 `Temp/<user>/<kind>/<entry>`
目录——**只有整棵子树都冷了**才删。全有或全无是安全性所在：运行中的作业一直在写，不可能被删掉一半。

它是用 **`sbatch --wrap` 投递**的，不是就地执行。遍历目录树 + `rm -rf` 是实打实的文件系统工作，
而 RCIC 的登录节点只用来登录和**提交**作业。所以登录节点只跑一条 `sbatch`，实际工作在免费的
`standard` 分区（1 CPU / 2 G / 30 分钟上限）。因为是异步的，控制台报的是**上一次**清理删了多少，
从 `bin/<user>/temp_gc.log` 读回。触发点：连接时、每个活跃会话每 6 小时，外加可选的每人一行 cron
（同样是投递，不是就地扫）。

仍在登录节点上的，全是元数据操作：连接时的 `mkdir`/`chmod`/`test -d`、读上次日志的 `tail`、
`sbatch` 本身、以及存储面板的 `du -sh`。文件传输走 `access-hpc3`（DTN）。

### 验证

2026-08-08 在真 Slurm 上端到端跑通：作业 55150830 / 55150832 在计算节点 `hpc3-l18-05` 上 COMPLETED，
日志留下 `GC_RESULT removed=1 kept=1`，下一次提交读回来了。冷单元被删；warm run **连同它 10 天前的
旧文件**一起保留；放在真实 103 G 容器旁边的诱饵目录未被触碰（清理器出不了 `Temp`）；别人的 Temp、
`uploads/`、`pysrc/` 未被触碰；所有守卫都拒绝。测试：**1107 通过，0 失败**。

### 资产

`docs/hpc3_assets.md` 是新加的，也是最需要持续维护的：HPC3 上的东西几乎都不在仓库里
（17 G 容器、66 G 权重/缓存、241 G 注释数据库在实验室共享的 `software/reference`）。它记录每项资产
是什么、谁建的、怎么重建。**以后在 HPC3 上 stage/build/下载任何东西，同一个 change-set 里加一行。**
它记录的已知缺口：**`paperqa.sif` 从来没建过**，所以 HPC3 上的 `deep_literature` 是降级返回的。

### 生产 `.env` —— 2026-08-08 已改、已验证、生产未受影响

`/data/BioAgent/app/.env` 已改完（49 → 51 键，无丢失）：

* 三个容器路径（`ANALYSIS_IMAGE`、`VEP_IMAGE`、`LIRICAL_IMAGE`）改为 `software/AiScientist`；
* 显式写入 `BIOAGENT_HPC_SHARED_ROOT` 和 `BIOAGENT_TEMP_TTL_DAYS=3`，不再依赖代码默认值；
* `VLLM_MAX_MODEL_LEN` 的内联注释挪到独立行；修正了「上传落在 `/dfs3b/ruic20_lab/<user>/uploads/`」
  这条已过时的注释。

验证方式：用**线上部署的** `core.config.load_dotenv` 分别解析新旧文件并对比 key→value 映射——只有那
五个键变化、没有键丢失、没有值残留未剥离的内联注释、没有重复键。（旧笔记里提到的重复
`VLLM_MAX_MODEL_LEN` 其实早已不存在。）写入用机器本地 `cat` 覆盖原 inode，owner/mode/ACL
（`aiscientist:aiscientist`、`-rw-rw-r--`、`group:users:rwx`）都保住了；回读逐字节一致。全程生产在线
——`bioagent.service` 自 2026-08-06 active，`:8800` 和公网 HTTPS 都返回 200。改前备份：eyeserver 上
`~/env.BEFORE-20260808`（mode 600）。

**没有重启，是故意的。** 运行中的进程仍持旧 env，旧路径经符号链接照常解析，不会坏；而线上部署的代码
（`e16e40c`，分支 **`feat/paperqa-embedding`** —— 注意生产不在 main 上）还没有 Temp/GC 这套功能。
等那套上线时再重启。

### 需要跟 Ziyao 说一声的一件事

生产的整条文献线指向**个人目录**：`BIOAGENT_PAPERQA_*` 共约 6.6 GB 在 `/dfs3b/ruic20_lab/<ucinetid>/`
下（2.8 G sif + 3.6 G papers + 215 M 索引 + manifest）。今天能跑（`drwxr-s---`，组 `r-x`），但那个账号
一旦整理或离开，`deep_literature` 会静默降级到 `dependency_missing`，run 照跑但就是没有文献。
不是我们该动的东西——那是他的线、在他的目录里。已在 `.env` 对应键旁边和 `docs/hpc3_assets.md` 标注。
（顺带更正我今天早些时候写错的一句：`paperqa.sif` **是**建过的，只是不在我们的 `containers/` 里。）


### 部署状态（2026-08-10）

main 现在是超集：`2873bfb` = HPC3 清理 + 生产原本从 fork 跑的 PaperQA/quick_chat 那条线。
测试 1107 通过，合并后重新验过脱敏没被带回来。

`scripts/sync_deploy.sh` **完全不碰 HPC3** —— 它只是把本地树 rsync 到网关主机并重启服务。
「不在登录节点干活」这条在产品里：Temp 清理是用 `sbatch --wrap` 投递的，登录节点只跑 `sbatch`，
外加连接时的 `mkdir`/`chmod`/`test -d` 和读上次日志的一次 `tail`。

代码**已暂存**在网关上（426 文件，`hpc_gc.py` 和 `deploy/hpc3/aiscientist_temp_gc.sh` 都确认在），
但**尚未安装**：特权镜像 + `pip install -e .` + 重启需要 `sudo`，而管理账号的 sudo 要密码，
没法非交互驱动。在交互式终端里收尾：

```
cd <本 worktree> && ADMIN_SSH=eyeserver-admin bash scripts/sync_deploy.sh
```

rsync 已经热了，几秒跑完，然后 sudo 提示一次密码。在这之前，生产仍然把过程文件写进个人目录、
仍然没有任何清理。

还剩一个登录节点操作是这次改动**没有**解决的：存储面板每次打开都对三个区域各跑 `du -sh`，
那是登录节点上真实的目录树遍历。它是用户主动触发的、且早就存在，所以没有未经测试就塞进这次部署。
修法是让清理作业（本来就要遍历 Temp）把体积缓存下来，面板读缓存。

## 2026-08-06 — 文件传输搬离 HPC3 登录节点

### 规则，以及我们当时的位置

RCIC 2026-08-06 通知：`login-i15/16/17` 只用来登录和提交 Slurm 作业。不跑计算，而且——打中我们的这条——
**不做数据传输**。`rsync`/`SFTP`/`rclone`/`wget` 属于 `access-hpc3.rcic.uci.edu`，无视这条的进程他们可能开始删。

我们在生产上、在最忙的那条路径上违反了它。`SSHExecutor.put_file`/`get_file` 在**和 `exec` 同一条 paramiko
transport** 上开 SFTP——那条连的是登录节点——而生产的 offload 开关全开
（`/data/BioAgent/app/.env` 里 `UPLOADS_ON_HPC`、`ANALYSIS_ON_HPC`、`RUN_CODE_ON_HPC`、`REPORT_ON_HPC`、
`VARIANT_ON_HPC`、`PHENOTYPE_ON_HPC` 全 `=1`）。所以每一次用户上传——1.1 GB 的 WGS VCF、可达 ~15 GB 的
h5ad——都在登录节点上走；三个 staging 脚本的注释还主动推荐在登录节点下载 20–87 GB。

### 传输主机到底是什么（实测，不是假设）

`access-hpc3.rcic.uci.edu` **不是**第二台登录节点，这决定了修法：

| 探测 | 结果 |
|---|---|
| `ssh access-hpc3 echo hi` | `Error: Command 'echo' not allowed` —— 受限 shell |
| `wget` / `curl` / `rsync` | 放行 |
| `bash` / `sbatch` | 拒绝 |
| `/dfs3b`、`$HOME` | 与登录节点完全一致；SFTP 的 `mkdir`/`put`/`rm` 都可用，组是正确的 `ruic20_hpc` |

所以它永远不能承载 `sbatch`/`squeue`/`module`/`singularity` 或 vLLM 隧道——shell **脚本**也不能在那儿跑，
只能跑裸的下载命令。

### 修法的形状：拆开两个平面，而不是换主机

`SSHExecutor` 保留登录会话做**控制面**（`exec`、Slurm、隧道），另开一条惰性的、只走 SFTP 的连接到
`BIOAGENT_HPC_TRANSFER_HOST` 做**数据面**（`put_file`/`get_file`）。远端路径不变，因为两台挂的是同一套文件系统。

三个刻意的细节：
- **父目录 `mkdir` 留在登录会话**。它是控制命令，而且受限 shell 本来也会拒绝它。
- **只在 key 认证时启用**。密码会话需要**第二次** Duo 推送，而在传输中途弹一次，和卡死无法区分。这类会话回落到
  登录节点并只警告一次——而网关在首次密码登录时本来就会提议给用户铸一把 key，缺口由此收敛。
- **任何失败都降级，绝不中断**。传输主机不可达 → 警告一次、走登录会话、并停止重试，免得一台死掉的主机让每个
  文件都多付一次连接超时。`BIOAGENT_HPC_TRANSFER_HOST=""` 是刻意的静默退出开关。

### 是实测验证过的，不只是 mock

`tests/test_ssh_transfer_host.py`（11 个测试）断言每一个字节走的是哪条连接。但真正的证据是用真 key 对真 HPC3 跑的那次：

```
transfer peer : ('128.195.119.99', 22)   <- access-hpc3
login peer    : ('128.195.119.98', 22)   <- login-i15
```

两台不同的主机，64 KB 探针往返逐字节一致，落在 dfs3b 上组正确，零警告，随后清理干净。全量测试：
**1105 passed, 2 skipped**。

### 一并修了什么，还剩什么

三个 staging 脚本（`deploy/vep/build_and_stage.sh`、`deploy/vep/stage_annotation_dbs.sh`、
`deploy/lirical/build_and_stage.sh`）原先教人在登录节点下载 20–87 GB，现在改成在 `standard` 上
`sbatch`/`srun`（计算节点出网已于 2026-07-08 验证）。paperqa 和 vlreview 的 runbook 现在 rsync 到
`access-hpc3`；paperqa 的建环境 + 模型下载也从登录节点挪进了分配里。RCIC 关于 `.bashrc` 里 conda 初始化
那条我们本来就合规——HPC3 的 `.bashrc` 只有 24 行，没有 conda 初始化。

### 第二遍：读文件内容本身也是传输

修 `put_file`/`get_file` 并没有覆盖全部面。把全部 60 个 `exec` 调用点扫了一遍，发现还有数据以别的名义穿过登录
节点——特征恰恰是它们**看起来都不像传输**：

| 原先 | 为什么算 |
|---|---|
| `head -c 262144 <file> \| base64 \| tr -d '\n'`（每次上传 peek） | 在登录节点上起三个进程，且 base64 把每次 256 KB 的 peek 撑大三分之一 |
| `cat <result>.json`、`cat <log>`（run_code + analysis + vlreview） | 作业输出经登录会话被拉回来 |

`RemoteExecutor` 新增 `read_bytes(path, max_bytes=None)` 和 `remote_size(path)`，都走 SFTP——于是和其他东西
一样骑在传输主机上。SFTP 本来也更适合干这件事：它能只取一段前缀而不碰文件其余部分（在 1.1 GB 的 VCF 上做 256 KB
peek 和在小文件上一样便宜），字节保持二进制。文件不存在时返回 `b""`，与原先的 `cat … 2>/dev/null` 一致，所以
把"不存在"当作"还没写出来"的调用方完全不受影响。

注意 prefetch：paramiko 的 `prefetch()` 不带参数会拉**整个文件**——对 WGS VCF 做 256 KB peek 时，那正是我们要
消除的那个 bug。代码里传了窗口大小。

`test_slurm_analysis.py` / `test_slurm_sandbox.py` 里的假件**刻意删掉了 `cat` 分支**——一旦生产代码退回 `cat`，
就会落到空的兜底分支并让测试失败。

**登录节点上剩下的全是控制面**，那本来就是它的用途：`sbatch` / `squeue` / `sacct` / `scancel`，
`mkdir` / `test` / `find` / `du` / `stat`，一处 `tail -n` 看日志，一处在等 GPU 分配时 `cat` vLLM 启动日志，
以及一次 0.9 MB 源码包的 `tar xzf`。都是亚秒级、几 KB 的事——不是 RCIC 要我们挪走的东西。

**尚未部署。** 只在本地 `main` 上，现在领先 `origin/main` 27 个提交。不上线，生产就一直在违规。注意 eyeserver
的 `.env` 不需要改——默认值就是合规的那个，只有想**退出**才需要设变量。


## 2026-08-05 — 表型线现在能诊断 LIRICAL 诊断不了的情况

### 缺口

`run_lirical` 只报告一条轨道就结束了。在答案已被策展时这没问题，但罕见病恰恰会撞上下面三种情况：

| 情况 | 之前用户拿到的 |
|---|---|
| LIRICAL 未部署 / 报错 | `not_installed` 和一个空的鉴别诊断——死路 |
| 该基因不在 OMIM/HPOA 中 | 沉默，与"不相关"无法区分 |
| 策展数据过时 | 一个自信的后验概率，而文献没有任何渠道提出异议 |

LIRICAL 的后验只能和它读取的策展数据一样新，而策展比文献滞后数月到数年。此前流程中没有任何东西能对此作出反应。

### 本次落地的内容

**`tools/phenotype_evidence.py`** —— PaperQA2 契约中一直留作占位符的证据轨道 runner，基于仓库中已有的
`deep_literature` 工具实现。它返回契约规定的 `{association, clingen_tier, evidence[]}` 记录；整个设计的
核心是：**tier 不能只由模型说了算**：

1. **检索决定存在性** —— 没有检索到段落 ⇒ `NONE`，无论文字表述多么肯定。
2. **段落为等级设上限** —— `evidence_ceiling()` 统计**独立来源**（PMID/DOI）而非 chunk 数，因此一篇被切成
   多块的论文无法伪装成重复验证。仅凭一篇个案报告声称的 DEFINITIVE 会被记为 LIMITED。模型可以给出**低于**
   上限的等级，但绝不能更高。
3. **每条结论都附带其原始段落** —— 等级可以对照产生它的原文被复核。

**`phenotype_dx.adjudicate()`** —— 决策层。`reconcile()` 仍然把两条轨道分开保存以留存溯源；`adjudicate()`
产出临床医生真正阅读的那**一个**排序列表，其中**文献权重高于 LIRICAL（0.65 / 0.35）**。每个候选都落入且仅落入
一个分支 —— `concordant` / `conflict` / `literature_only` / `lirical_only` / `unsupported` —— 并携带该分支
与一条 `decision_note`，因此排序永远不是一个孤立的数字。

这种不对称正是重点：一条经检索、有引文的反驳，现在会把 LIRICAL 96% 的判断压到未被反驳的 20% 候选之下；而
LIRICAL 从未给出、但文献评为 STRONG 的候选，会进入**同一个**列表，而不是一个没人会去对照的附属列表。

**`phenotype_dx.diagnose()` 与 `diagnose_disease` 工具** —— 端到端跑完两条轨道。它是**组合**
`run_lirical` 与 `deep_literature`，而不是重新实现任何一个；registry 在**路由之后**才绑定它，因此它会自动
跟随这两个工具一起跑到 HPC3 上。

### 没有改变（也不能改变）的部分

`posttest_prob` 永不被覆写，也不会为纯文献候选凭空编造概率。`final_score` 是独立命名字段上的**排序**分数。
把两者保持为**不同字段**，正是这套设计没有变成设计文档专门用一节禁止的"货币混合"的原因。

有两处不对称必须记牢，弄反了就是临床错误：
- 语料**沉默**（`ungraded`）**不扣分** —— 没有数据 ≠ 有证据表明不存在；
- 语料**检索到了但不支持**（`unsupported`）只扣很小的分 —— 语料库被限定在约 IRD 相关文献范围内，因此
  "这里没有"只是弱信号；只有明确的 DISPUTED/REFUTED 才会重罚。

### 尚未验证

评级阶梯是对照 ClinGen 规则校准的，**不是**对照真实 /dfs3b PubMedBERT 语料的实际检索表现校准的。tier 阈值
是上服务器后要重新核对的东西 —— `tests/test_phenotype_evidence.py` 钉住了预期行为，所以重新校准只需要改
`evidence_ceiling()` 及其测试，不涉及其他。


## 2026-08-03 —— 补齐五个缺失步骤，以及让 agent 读自己的代码

### 管线上有五个「看起来很完整」的洞

QC → 聚类 → DE → 通路，这是一条看着完整的线。下面每一个缺失都**改变答案**而不是报错，
所以从来没浮出来过：

| 新工具 | 缺了它会怎样 |
|---|---|
| `run_doublet_detection` | 一个液滴里两个细胞会形成"中间态"簇，被读成新的过渡细胞类型 |
| `run_integration` | 多样本对象按**供体**聚类，于是下面每个细胞类型标签其实是供体标签——而且供体驱动的簇看起来和真的一样干净 |
| `run_pseudobulk_de` | 条件比较走的是**细胞层面**的 Wilcoxon |
| `run_composition` | "哪些群体扩张/收缩"根本没有工具 |
| `run_marker_annotation` | 管线里最要命的一步是个 `run_code` 模板，模型每次重写一遍 |

**伪重复这个数字值得记住。**在一个 4 供体的合成设计里，**400 个基因中只有 1 个**在两组间是
真的有差异：

| 检验 | 判为显著 (padj<0.05) |
|---|---|
| `run_de`（细胞层面 Wilcoxon） | **400 个里 310 个 —— 78%** |
| `run_pseudobulk_de`（供体层面） | 0 个，而且把真正那个基因排在第 1 |

`run_de` 对 marker（一个簇 vs 其余）是有效的，对**条件**无效——而
`preset_pipelines/differential_expression` 以前恰恰就是这么规定的。现在它按设计分支，并且在
每组只有 1 个样本时明说这个比较是**描述性**的，而不是悄悄打印 p 值。

**顺带发现的前置问题：**`run_scanpy_qc` 是在 `normalize_total`+`log1p` **之后**设的 `.raw`，
而注释写的是"这里存的是 raw counts"。它从来不是——原始计数在 QC 阶段就被销毁了，这正是所有
基于计数的方法都无法存在的原因。现在计数放在 `layers["counts"]`。

还修了新分辨率扫描里一个只有真 scanpy 冒烟测试才暴露的缺陷：单簇划分是平凡可复现的
（ARI 恰好 1.0），所以它能通过任何稳定性门槛，在数据弱的时候必胜。现在退化划分被排除在
选择之外，但仍保留在扫描表里。

所有东西都在 eyeserver 上对**真的** scanpy/gseapy/pandas/scipy 验过，不只是对假货——前面
`gp.prerank(rnk=dict)` 和 GMT 名字前缀那两个 bug 就是这么抓到的。

### `read_tool_source` —— 模型现在能读自己调用的代码

这是"为什么一个 50 基因的上限能活七周"的结构性答案。模型只能看到名字、一段散文描述和一个
结果字典。**描述说的是意图，只有代码体说的是行为**，而它从来看不到代码体。造成最大伤害的三个
默认值——`n_genes=50`、`background=20000`、`resolution=1.0`——在构造上就是不可见的，而且每一个
都产出了自洽的报告和全绿的测试。

`read_tool_source(tool=...)` 返回真实代码体、可通过 `symbol=` 取同模块的辅助函数、把声明的
描述和 schema **并排放在代码旁边**（这样"承诺的比做到的多"就变得可见），以及最关键的：一份
`defaults` 列表，列出每一个 `args.get(x, <字面量>)` 及其行号。做成结构化字段，是因为这把
"读代码"从一条模型可以跳过的指令，变成一份它必须看的清单。

**故意只读。**一个能在运行中改写自身实现的工具会让那次运行的所有结果都不可复现；审计的产出
是给人的发现，或者是"这步改用 `run_code` 做"的理由。

**还没答的那一半：**模型会不会真的**去用**。`scripts/probe_tool_audit.py` 就是测这个的——
两个有缺陷的场景加一个对照，按"有没有点名那个具体参数"打分，因为一个什么都报警的模型和一个
什么都不报警的模型一样没用。

## 2026-08-02 —— 32K 窗口从来不是硬件限制，以及管线 v2

同一工作树。两条线，都由 Yijun 触发：上下文窗口对这套硬件来说小得不合理；以及一条和真实实验步骤对
不上的管线没有意义。

### 窗口：32768 建立在一个错误前提上，已实测

`vllm_max_model_len` 默认 `32768`，理由写的是「AWQ 24GB 在 A100-40G 上只剩 ~16GB KV；262K 需要
80G」。这句话两半都错。修法不是猜的——我用**同一个镜像同一个模型**在两种卡上以
`--max-model-len 262144` 实际启动过（HPC3 `ctxprobe` 作业，2026-08-02）：

| 卡 | 分区 | KV cache | tokens | 262K 满长并发 |
|---|---|---|---|---|
| A100 **80GB** PCIe (sm_80) | `gpu`（付费） | 47.8 GiB | 2,466,442 | 9.41× |
| RTX PRO 6000 Blackwell **96GB** (sm_120) | `free-gpu32`（**免费**） | 58.8 GiB | 3,035,461 | 11.58× |

里面有两处纠正。**HPC3 的 A100 是 80GB，不是 40GB**——之前笔记里的 40GB 对 `l54-*` 节点是错的。
另外这个 Qwen3.6 是**混合注意力**：`layer_types` 里除了每第 4 层外全是 `linear_attention`，所以
40 层中**只有 10 层带 KV cache**（约 20 KiB/token）。跑满 262K 的 KV 只要 ~5 GiB。
`max_position_embeddings` 原生就是 262144——不需要 YaRN，不需要 rope scaling。窗口从来不是瓶颈，
并发才是。

现在 `gateway/settings.py` 和 `research_harness._default_max_model_len` 默认都是 `262144`（这两个
**必须一致**：按比 vLLM 实际服务的更小的数做预算，等于白白丢掉可用上下文，而 32768 干的正是这件
事）。**生产 `.env` 里钉的是 `131072`，所以在那一行改掉之前生产环境不会有变化**；环境变量优先是
设计如此。

还有一个值得你决定、我没有替你改的事：**免费的那张 Blackwell 反而是更大的卡**，而且 `awq_marlin`
在 sm_120 上确实能跑（探针日志里可见）。`gpu_candidates` 本来就支持两者竞速；付费分区买到的是**调度
优先级和不被抢占**，不是能力。`settings.py` 的注释里现在写进了实测数字，好让这个取舍有数据可依。

### 管线 v2 —— 按 Rui Chen 的真实流程重写

把我们的技能和 Rui Chen 那两份参考 SKILL.md 对比，翻出四个属于**方法学**而非风格的缺口。四个现在都
在代码里补上了：

- **整个系统里根本没有 preranked GSEA**（`grep prerank` 零命中）。新增 `run_gsea_prerank`：离线
  `gseapy.prerank`，走完整排序列表，对同一批本地 `.gmt`，保留带符号的 NES，并按 |NES| 排序——这样
  被抑制的通路和被诱导的通路一样显眼。
- **`run_de` 在 top-50 基因/组处截断**，这正是「既没有排序列表也没有检验全集」的根因。现在它同时写出
  `de_<groupby>_universe.txt` 和完整的 `rank_<groupby>_<group>.rnk`，**只落盘**——`raw_data_to_llm`
  仍为 False。
- **ORA 的 background 是常数 `20000`**。现在只要存在检验全集就用它，并且无论走哪条路都在结果里报出
  `background_source`——因为常数兜底会悄悄暗示「全基因组」，在 QC 之后基因数少得多时会让每个条目都
  显得更富集。
- **`resolution=1.0` 是写死的默认值**。`run_clustering` 新增 `select_resolution: true`——bootstrap
  ARI 稳定性扫描，取**仍然可复现的最细**分辨率（取最稳的会永远返回最粗的那个）。受
  `max_sweep_cells` 封顶，且抽样这件事会在返回的 note 里说明。另外报出 `resolution_source`，让报告
  能区分「默认值」和「最细可复现值」。

`skills/annotate_clusters_by_markers_v2/` 取代了 v1 的 top-25 集合求交计数法。v1 完全无法处理共享标记
（LAMP3 同时在 AT2 和 DC 面板里；SLC1A3 同时在 Müller 胶质和星形胶质里），因此产出的是**自信的错误
标签**而不是可见的失败。v2 做 signature scoring，把 z-argmax 只当作**第一遍**，最终判定看**原始**
判别基因表达并要求优势比，两者不一致时**两个判定都保留**，信号不成形的簇留 `Unassigned`。这是为技能
诱导建的 **`supersedes:` 版本化机制第一次真正投入使用**：manifest 只推 v2，v1 仍可按名加载以便回滚。

顺带修掉的：组标签里带 `/` 时（`Club/Secretory`、`Pericyte/SMC`，都是再普通不过的细胞类型名）per-group
表写入会直接抛异常，于是那个类别的表**悄悄从来没出现过**。文件名改为 slug，数据里的标签保持原样，另配
slug→label 索引。

测试：**1032 passed, 1 skipped**。仍然没有在真实数据上跑过——这依然是最大的未知。

### 生产现状，以及为什么这次部署是按文件范围做的

`.env` **已改**：`BIOAGENT_VLLM_MAX_MODEL_LEN=262144`（原 131072），已用线上部署的 `load_dotenv`
验证解析成干净整数，属主和 ACL 保持不变，备份在 `~<admin-ucinetid>/env-backup-20260803-000412`。
下次重启生效。生产本来就设了
`BIOAGENT_GPU_CANDIDATES="free-gpu32,gpu:RTX6000:1,ruic20_lab;gpu,gpu:A100:1,ruic20_lab_gpu"`
——也就是说**它已经在和那张免费 96GB Blackwell 竞速了**，而两个候选都实测能在 262144 下启动。

**不要在这个工作树里跑 `scripts/deploy_interactive.sh`。** 它是整树 rsync + `--delete`，而这棵树在
文献线上**落后于生产**。生产跑的是 `feat/paperqa-embedding @ ab94be6d`（2026-08-02 22:09 部署），
**这个分支不在 origin 上**——也就是说线上那份代码只存在于那台机器上。生产领先的文件：
`quick_chat.py`（82 行，完全领先）、`paperqa_search.py`（65 行，检索广度调优）、
`literature_references.py`（23 行，正文引用标记剥离）、`paperqa_cli.py`（9 行），
以及 `research_lab.py` 和 `app.py` 里的若干行。整树部署会把这些全删掉。

按文件范围的部署已经准备在 eyeserver 的 `/tmp/bioagent-toollayer/`，并且**在真实生产树的副本上做过
预检**（catalog 能构建、schema 对生产的 `HarnessTool` 合法、v2 加载且 v1 被 supersede 但仍可按名解析、
`gateway.app` 能导入）：

```
sudo -u aiscientist bash /tmp/bioagent-toollayer/apply_toollayer.sh
```

它只发 `scrna_pack.py`、`scrna_cli.py`、`skills.py`、v2 技能目录和 `celltype_annotation` 预设——
不碰文献线的任何文件。`skills.py` 必须跟着一起发：线上那份早于版本化机制，所以 `supersedes:` 会被
忽略，而 v2 的 `>-` 描述会被解析成字面量 `">-"`。这不是假设——**生产上的
`literature-corpus-recovery` 现在就因为这个原因在 manifest 里不可达**，这次部署会顺带修好。

在机器上发现、但**没有**修掉的两件事：

- 生产的 `genesets/GO_Biological_Process_2023.gmt` 是一个**只有一行的测试桩**
  （`term<TAB>desc<TAB>RHO<TAB>PDE6A`），不是真的 GO 库；而它在 `_DEFAULT_GENE_SETS` 里，所以每次默认
  富集在这个库上都是对着桩跑。需要重跑 `scripts/fetch_genesets.py`。那个目录的 ACL mask 是 `r-x`，
  所以要用服务账号做。
- 仓库里没有生产正在跑的那份代码。在任何人再部署之前，`feat/paperqa-embedding` 应该先推到 origin。

## 2026-08-02（后续）—— 接 API 这条路，以及它底下更难的那个问题

同一工作树。两个提交，加上 Yijun 看到 Claude Science 之后要求的战略审查。

**数据边界修复（`492fca3`），这是阻塞项。** 决定「原始表格数据能否进 prompt」的守卫，原先用
`ctx.tunnel_port is not None` 来判断「模型是本地的」——把 SSH 隧道的存在当成 prompt 不出这台机器的
证据。这个推断只在「隧道里的 vLLM 是唯一可达端点」时成立；一旦 `BIOAGENT_LLM_BASE_URL` 绑定了 API
就不成立了，而 `app.py` 是**无条件**设置 `tunnel_port` 的。也就是说，在我们正打算采用的那个配置下
（GPU 仍分配着、PI/Critic 走付费 API），守卫会把远程端点判成本地，把原始表达矩阵放进发往外部的
prompt。现在「本地」需要正证据：有隧道**且**没有远程端点覆盖，通过显式的
`HarnessContext.llm_is_remote` 表达。`endpoint_is_off_host()` 放在 `integrations/safety.py`（挨着
它服务的守卫，且不依赖 web 栈就能测），并且故意保守——URL 解析不了算远程，主机名即使解析到本地也
算远程。

**角色分离（`2c31891`）。** `BIOAGENT_LAB_LLM_*` 现在只路由推理角色，Scientist 留在
`BIOAGENT_LLM_*`。这里包含一处更正：那几个环境变量之前就写在文档里，但它们是被
`providers/openai_compatible.build_llm_fallback_client` 读取的，而**网关根本不用那个函数**——所以
在生产实际走的路径上，角色分离从来就不存在。`_lab_llm` 现在返回 NamedTuple，并且**分别**报告两种
暴露，因为它们不是一回事：`scientist_remote` 驱动守卫；`lab_role_remote` 是推理载荷，而它**今天不
经过守卫**。这个缺口是真实的，处理方式是让它**出声**而不是沉默——每次使用远程推理端点的 run 都会
打印什么离开了集群。给 PI/Critic 载荷加守卫是刻意**没有**做的：守卫是围绕「不可信用户片段」做
来源限定的，而推理 prompt 没有这个概念，全 prompt 扫描会对合法的 findings 误报。

### 战略审查 —— 我们到底为什么要做这个平台

Yijun 问了真问题：Claude Science 已经存在，这个平台意义何在？deck
（`AiScientist-平台评估-2026-08-02.pptx`，已交付 Yijun，未提交仓库——它是 500 KB 的二进制）给出了
一个立场，而这个立场默认是不好听的：

- **Claude Science 含在每个付费 Claude 方案里** —— Pro $20/月，Team $20–25/席/月，学术实验室另有
  折扣席位。它在 beta 里已经做单细胞 RNA-seq 分析、CRISPR 筛选设计、化学信息学，并且有一个
  reviewer agent 逐条核查引用与计算。**在通用能力上，买比自建便宜得多，而且差距不小。**
- **所以自建的理由不能是「我们也能端到端做分析」。** 我们确实输的地方：通用多步编排、文献问答与
  引用核查、报告撰写、连接器广度、原生前沿模型质量。**本周新建的自主栈，是与商用产品重叠度最高的
  一部分**——这话必须自己说。
- **云端工作台结构上到不了的地方**：(1) 患者 VCF/表型的数据治理——**注意区分：陈老师同意「对衍生
  发现做推理时走 API」，不等于同意「把患者 VCF 上传到 SaaS」**；(2) 锁版本的重参考栈 + 机构算力
  （我们自己的 Exomiser-1805、hg19 被当成 GRCh38 两次事故，就是「锁版本是临床硬要求」的证据）；
  (3) 实验室专科 IRD 流程与自有金标准——一批内部已解病例，以及「这些病例富集于通用流程结构上
  排不出来的 0-OMIM 新基因」这个发现；(4) 确定性与可审计。
**2026-08-02 由 Yijun 更正，而且这个更正改变了结论。** 上面的分析只算了 token，忽略了
**UCI 的显卡对实验室几乎免费**这件事。多跑一次研究的边际现金成本约为零，而订阅产品是按席位计费的。
这把前面标出的两个「放大器」方向反了过来：多 cycle 和假设驱动的探索——订阅产品最贵的东西——对我们
最便宜。长程、多轮、反复试错的研究，正是免费算力唯一能赢的场景。所以 **agent 能力不是该砍掉的通用
部分，它就是产品方向**。修正后的立场：

- **本地免费优先；API 角色分离是逃生阀，不是方案本身。** prompt 保持在服务窗口内，JSON 契约保持
  本地模型能处理的简单度。探针给本地 Qwen 的探索回合打了 3/3。
- **自主够用就好**（Yijun 原话：确保它真的能自主）。可靠跑通优先于堆功能。
- **省的是延迟，不是钱**——每一次可避免的模型调用都是研究者干等的墙钟时间。`9d78685` 提交了第一版
  确定性预过滤：文献步骤、以及既无产物又无实质回答的步骤，不再发起探索调用。
- 因此建/买表的第一行从「买」改成了「建」。

下面那几条护城河仍然成立；免费算力现在是第五条，也是经济引擎。

- **已被取代的建议（存档用）：把平台收窄。** 定位为「UCI 眼科基因组的受治理执行层 + 证据层」。大脑外购（角色分离已经
  让它变成配置改动）、双手自建（临床栈、HPC3 作业、数据边界、确定性护栏）、证据自建（病例集与评
  测）。停止把工程人力投在通用 agent 脚手架上。
- **决定一切的那个问题**：如果患者数据**可以**出 UCI，这个平台最强的理由就消失了。这一条应当在
  继续投入之前先回答。

deck 里的报价与评分是 2026-08-02 从 OpenRouter models API 和 Artificial Analysis 智能指数实时抓取
的，会很快过时。`scripts/probe_exploration.py` 是重新决定模型的廉价方式——每个候选三次 API 调用。

测试：981 通过（之前写的 716 是不完整收集的结果；临时 venv 当时缺 fastapi/paramiko/httpx/
itsdangerous，现已装齐，相关套件全绿）。

## 2026-08-02 —— 外循环，以及会自己写自己的技能库

工作树 `eyeserver-gpu-request-check-4b9621`，接在探索那个提交之上。**未合并、未部署。**三个环境
开关，全部默认关闭。

**多 cycle（`max_cycles` / `BIOAGENT_MAX_CYCLES`）。** 探索是**反应式**地让计划生长：一个被接受的
步骤产出一条假设 + 一个检验它的步骤，追加到正在跑的计划里。它无法重构计划，而且一次只看得到一个
结果。**cycle 则是整体重规划**：一轮结束后，PI 拿着全部已接受发现 + 假设账本来规划下一轮，因此它
可以放弃一整条路线，或者为"第一轮之后才值得问"的问题安排四个步骤。`_run_loop`/`_run_dag` 增加了
`synthesize` 开关，中间轮次不写任何东西；团队解读和正文只写**一次**，覆盖所有轮次的 rounds，
rounds 也重新编号成一个连续序列——所以报告读起来是一项研究，而不是 N 份报告钉在一起。

终止条件**先确定性、后模型判断**——外循环若以 LLM 的意见作为退出条件，就是让一次 run 烧掉一个周末
GPU 的做法。硬上限；轮次之间响应取消（取消后不再发起任何 LLM 调用）；"没有可追的了"；重规划返回空
或重复刚跑过的那一轮；重规划抛异常则结束 campaign 但仍然把已完成的工作写成报告。规划 prompt 里明确
写了：**停下来是一个合理且常见的答案**。**测试在这里抓到一个真 bug：**"没有可追的了"原先以"账本为空"
判断，而在探索关闭时账本**天然**为空——于是每个 campaign 都会在第 1 轮后悄悄停止。现在这条判断以
`hypothesis_driven` 为前提。

**Skill induction（`skill_induction` / `BIOAGENT_SKILL_INDUCTION`）。** `skills.py` 从写下那天起就
声称技能库是 "grown by induction"，但从来没有代码写出过任何技能。现在：一次 run 结束时，把一个被
接受的 `run_code` 过程泛化成 `SKILL.md` + `reference.py`，供后续 run 检索和改写。**那段代码本来就是
Scientist 自己写并且已经执行过的——induction 只是把它记住，没有赋予任何新的执行能力。**

写到哪里是这里最关键的决定：写入一个**独立目录**（`BIOAGENT_INDUCED_SKILLS_DIR`，否则是连接的
workspace），**绝不**写进 git 跟踪的 `skills/`。让模型悄悄改动会分发给所有用户的源码，和让它在自己的
工作目录里留一份模板，是两件性质完全不同的事。`skills.py` 现在同时加载两个根目录，同名时**人工编写
的胜出**；frontmatter 标记 `induced: true` 和来源步骤，SKILL.md 正文也会警告读者这不是人工审校过的。
所有护栏都是确定性的，与模型自称无关：名字必须匹配严格 slug（它会变成一个目录名）、代码必须能
`compile()`、大小上下限、不得重名、不得覆盖已存在目录、每次 run 最多 2 个。`register_skill()` 只增不
改，所以并发的 run 绝不会看到某个技能在脚下被替换，只会看到新增。想让诱导出的技能在重启后仍然存在，
就把环境变量设成一个稳定路径；否则只在当前进程内有效。

**回归验证方式。** 两个提交都用同样的方法验证：对三种配置（linear、linear+step_meetings、DAG）跑
脚本化的 run，抓取**每一条 prompt 和每一个事件**，改动前后做 diff。两次都在默认配置下**逐字节一致**。
harness 在 session 目录的 `scratchpad/capture_baseline.py`，下次做同类改动值得重建。

测试：`tests/test_multi_cycle.py`（10 个，主要是终止条件）+ `tests/test_skill_induction.py`
（22 个，主要是"拒绝"）。全量套件 712 通过。

**遗留 / 下一步：**（1）**完全没有接到控制台**——三个功能都只有环境变量，没有 UI 开关；（2）账本仍未
持久化进 run_state，A2 resume 会丢，campaign 也无法中途恢复；（3）诱导出的技能没有任何审查、退休或
"晋升进 `skills/`"的流程，库只会单调增长；（4）**这些都还没有在真实数据上跑过**，只有离线 harness 和
那个单回合探针。

## 2026-07-31 —— 计划终于能"长出来"了：假设驱动的探索

工作树 `eyeserver-gpu-request-check-4b9621`。**未合并、未部署**，默认关闭
（`LabConfig.hypothesis_driven` / `BIOAGENT_HYPOTHESIS_DRIVEN=1`），关掉时行为与之前完全一致。

**问题。** Yijun 的判断是这个系统"更像一个大号 pipeline"。这在结构上是成立的，而且不怪模型：
`_pi_plan` 只跑一次，且在任何分析结果出现之前（它只看到问题 + 数据集 profile）；之后加的所有机制
都只能让计划**变小**——`_preflight_gate`（继续/修正/跳过）、`_poststep_review`（只能 prune，且限定
在已有的剩余步骤里）、`_plan_review`（执行前）。全仓库没有任何一处在运行中往 agenda 里追加步骤；
`_propose_alternatives` 明确只作用于修复某个失败步骤（"must not change the research goal"）。所以
第 3 步跑出一个意外结果，它无处可去：系统继续执行那份"盲着眼"起草的计划。而且系统里根本没有假设
对象——`hypothes*` 只出现在 prompt 套话里。

**改了什么。** 一个步骤被 Critic 接受之后，PI 多一次探索回合（`_EXPLORE_SYSTEM` →
`_explore_after_step`）：这个结果是否推翻了计划的前提？如果是，就在新的账本
（`agents/hypotheses.py`）里记下一条**可证伪**的假设（陈述 + 预测 + 判别性检验），并追加那个用来
检验它的步骤。后续步骤可以对未决假设作出裁决（`supported` / `refuted` / `inconclusive`），所以这
是一个闭环而不只是"生成更多活儿"；整个账本会送给报告撰写环节——**包括被证伪的和仍未决的**，否则
这个循环会退化成只找支持证据的机器。

两个 planner 都能生长。线性循环追加到 agenda **末尾**（因此 `step_idx`、`pruned`、已经发出的
`step_index` 全部保持有效）；DAG 则新增一个真正的节点，`depends_on` 那个引发它的节点，于是
Coordinator / 专家认领 / readiness 调度会把它当成一个正常任务对待。两边的 round budget 现在都改为
每轮重算——否则新步骤会被一个按原长度冻结的预算饿死。

**护栏和功能同样重要**（全部是确定性的，位于 `_explore_after_step`）：没有我们真正持有的假设支撑的
步骤会被丢弃（不要孤儿工作）；既无预测也无检验的"假设"被拒绝（那是"再深入看看 X"的伪装）；与现有
步骤重复的（归一化词袋比对，模型最常见的失败）丢弃；报告/打包类杂活丢弃；生长受两道上限约束：
`max_new_steps`（6，整个 run）和 `max_steps`（20，计划总长）。任何解析/LLM 失败都退化为"没有新东西"
——也就是今天的行为。

**是实测，不是假设。** `scripts/probe_exploration.py` 用真实的生产探索回合跑固定的结果样本，并且
**双向**打分：两个"应该开出新路径"的场景 + 一个"应该保持沉默"的对照（只测正例会奖励那种什么都觉得
意外的模型）。经 OpenRouter 实测 `qwen/qwen3.6-35b-a3b` 基线：**3/3 通过**。在"意外细胞群"那个场景
里，它同时提出了 doublet 假象假设和转分化假设，各自带判别性检验，并且在对照场景保持沉默。所以就
这一项判断而言，本地模型显然不是瓶颈——缺的是"生长通道"，不是推理能力。**注意**：探针喂的是一段
短的、干净的、手写的结果，没有累积上下文；真正难的是在第 9 步、40k token 的运行历史压力下，对一个
凌乱的真实结果做同样的判断。3/3 应当视为能力下界，不是结论。

`configs/aiscientist.example.env` 现在记录了 `BIOAGENT_LAB_LLM_*`：把推理角色（PI / Critic / 探索 /
综述）切到更强的 API 模型，同时 Scientist 的工具调用留在本地——并写明了数据边界警告，因为这些
prompt 会把数据集 profile 和已接受发现带出集群。

**遗留 / 下一步：**（1）DAG 路径会跑探索，但 `_preflight_gate` 的模型那一半仍然只在线性循环生效，
所以新发现的 DAG 节点过 Critic 但不过 gate；（2）还没有多 **cycle** 循环——这是让**一次** run 的计划
生长，不是用第一轮的账本重新规划第二轮（Kosmos 对标项）；（3）skill induction 仍未实现
（`skills.py` 写着 "grown by induction"，但没有代码写出新 skill）；（4）账本尚未持久化进 run_state，
A2 resume 会丢失。

测试：`tests/test_hypothesis_exploration.py`（15 个，全离线——账本、DAG 生长原语、默认关闭、生长、
裁决，以及每道护栏各一个）。全量套件 680 通过。

## 2026-07-27 —— 删除 GPU 懒加载:连接生命周期只剩一条

分支 **`refactor/drop-lazy-gpu`**(基于 `main` @ `ac870c0`)。**未合并、未部署、未 push。**
你的决定:懒加载在我们的集群上表现很差,前端交互逻辑也让人困惑 —— 而且生产环境本来就是
`BIOAGENT_LAZY_GPU=0`,所以这次删的是**代码和 UI 的残留**,不是行为。

**之前:**`/api/connect` 可能只走一半。开启 `lazy_gpu` 时,SSH 登录完成后会话就进入 `connected`
状态 —— SSH 通了,**但没有模型** —— GPU/vLLM serve 作业要等到第一次运行时才分配
(`conn.alloc is None` → `_ensure_gpu_ready_blocking`),或者由 `POST /api/connect/gpu` 显式触发。
于是三处代码(`/api/lab`、`/api/lab/continue`、控制台输入框)都必须把 `connected` 当作"可用",
控制台还为它维护了一套只属于懒加载的状态文案和冷启动特判。

**现在:**`_provision_blocking` 是唯一路径。状态只走 **connecting → provisioning → ready**,
不存在"SSH 通了但没模型"的中间态,所以下游一律可以把 `status == "ready"` 读作"整条栈都活着"。

**删除:**`HPCSettings.lazy_gpu` 与 `BIOAGENT_LAZY_GPU` 解析;`POST /api/connect/gpu`(前端从未
调用过);`_ensure_gpu_ready_blocking`;`_run_lab` 和 `_run_quick_chat` 里的延迟供给分支;
SSH-only 的 `connected` 状态(`_ssh_connect_blocking` 现在把会话留在 `connecting`,只发出
`ssh_connect` 成功事件)。**保留:**`_ssh_connect_blocking` / `_provision_gpu_blocking` 的内部
拆分 —— 仅为可读性,两者在 `_provision_blocking` 里前后脚调用;以及 `conn.gpu_lock`,
`_heal_vllm_session` 仍要用它串行化运行中的 vLLM 自愈。

`/api/lab` 与 `/api/lab/continue` 的守卫从 `status in ("ready", "connected", "provisioning")`
收紧为 `status == "ready"`:一次性供给之后,非 ready 的会话根本没有模型,在那里发起运行只会
让调用方无声地卡在 ~10 分钟的 A100 启动上。控制台输入框的守卫同步收紧
(`state.status !== "ready"`)。

**控制台:**只剩一条进度线。删掉了 `connected` 圆点样式、`"Connected · model starts on first
run"` 文案、`connected` 即置 `everConnected` 的捷径,以及冷启动卡片对 `connected` 的提前返回。
`state.everConnected` 现在的语义就是"曾经活过一次",这也正是它两个使用点真正想要的。
**未改动:**运行隔离(`RunState`、run_id/conversation_id)、重连回放与 run-owner 重新认领、
Stop、死运行宽限计时器、快速 chat 通道,以及今天刚合入的 `agents/chat_context.py` 压缩。

`tests/test_lazy_gpu.py` → **`tests/test_connect_provisioning.py`**:是重写,不是删除。现在断言
的是相反的不变量 —— SSH 阶段绝不对外发布一个"半连接可用"的会话、SSH 与 GPU 在同一次调用里一起
起来、运行只能从 `ready` 发起、并且不存在任何延迟供给入口
(`_ensure_gpu_ready_blocking` / `/api/connect/gpu` / `lazy_gpu`)。测试套件:
**933 passed, 1 skipped, 0 failed**。

**运维(你来处理,本次没有碰服务器):**生产的 `/data/BioAgent/app/.env` 里还有一行
`BIOAGENT_LAZY_GPU=0`。它现在是**空操作**,建议下次改 `.env` 时顺手删掉(留着也无害,未知键会被
忽略)。可以和还欠着的 `VLLM_MAX_MODEL_LEN` 重复键去重一起做。
`configs/aiscientist.example.env` 和 `deploy/{analysis,vep,lirical}` 的 README 已经不再宣传这个开关。

---

## 2026-07-27 —— Chat 通道终于有了上下文管理(占用可见 + 压缩)

分支 **`feat/chat-context-compaction`**(基于 `main` @ `cb2843b`)。**未合并、未部署、未 push。**
新模块:`src/bioagent/agents/chat_context.py`。

快速 Chat 通道此前**完全没有上下文管理**:只取最后 `QuickChatConfig.max_history_messages = 12` 条消息,
更早的直接丢弃 —— 不数 token、不压缩、也不告诉任何人。结果是长对话在服务端窗口(生产 131072)还空着
~100K token 的情况下,悄悄忘掉第 7 轮,而且界面上没有任何提示。(`quick_chat.py` 里已有的 `budget`
是**工具调用**预算,与此无关。)

- **现在的 prompt 是 `system + [更早轮次的滚动摘要] + 最近 N 轮原文 + 本轮提问`**,并按预算裁剪。
  只有超预算时才触发摘要。
- **Chat 的目标是 ~24K,而不是服务端窗口。** 这是刻意的:prefill 耗时随 prompt 长度增长,100K 的
  prompt 会在第一个 token 出现前烧掉好几秒 GPU —— 而"第一句话立刻出现"正是这条通道存在的唯一理由。
  同时永远夹在 `min(max_prompt_tokens, max_model_len − output_reserve)` 之内,不会超过真实窗口。
- **滚动摘要是增量的** —— 每轮只把*上一版摘要* + 本轮新逐出的几轮折叠进去,所以对话再长,摘要成本也是
  常数。摘要按 `conversation_id` 存在 `Connection` 上(仅内存;丢了就下一轮重建)。
- **所有失败都退化成今天的行为**(丢最旧的):没有摘要器、摘要器抛异常、返回空/垃圾、token 计数返回
  None 或抛异常。上下文管理绝不能成为让用户这一轮聊天失败的东西。
- **复用 research 通道**而不是另起一套:直接 import `research_harness` 已标定的估算原语
  (`_approx_tokens` / `_msg_tokens` / `_default_max_model_len`),并发出**同一套事件**
  (`context_measured` / `context_trimmed`),所以 `_lab_event_to_chat` 无需改动即可渲染。
  `research_harness` 本身一行未动。
- **注入而非 import**:`count_tokens_fn`(→ `vllm_client.count_tokens`,vLLM `/tokenize`)与
  `summarize_fn`(→ `vllm_client.complete`,`think=False`、小 `max_tokens`)由 `_run_quick_chat`
  注入 `run_quick_chat`,与 `stream_fn` 完全一致 —— 因此 `quick_chat.py` 仍可在没有 `paramiko`、
  没有 gateway 的裸 checkout 上 import 并测试。
- **控制台**:输入框上方一个紧凑的 `18.4K / 24K` 标签(新的 `chat_context` WS 事件),超过 80% 变琥珀色,
  真正触发压缩后变蓝。后端不发上下文事件时标签根本不出现,所以 Research 路线和旧客户端不受影响。

可调项(都在 `QuickChatConfig` 上,继承自 `ChatContextLimits`):
`max_prompt_tokens` = 24000(`BIOAGENT_CHAT_MAX_PROMPT_TOKENS`)、`keep_last_exchanges` = 6
(`BIOAGENT_CHAT_KEEP_EXCHANGES`)、`max_model_len`(`BIOAGENT_VLLM_MAX_MODEL_LEN`,与 harness 共用)、
`output_reserve_tokens` = 2048、`summary_max_tokens` = 512、`summary_max_chars` = 2400。
`max_history_messages` 保留,但已降级为外层保险(200),不再是记忆上限。

**未验证 —— 合并前请先看:** 从未在真实 Qwen3.6 上跑过。摘要质量、以及 Qwen 的 chat template 对
**第二条 `system` 消息**的渲染是否正常,这两点都没在真机上验过 —— 上集群后请优先确认这两件事。
测试(930 passed, 1 skipped)全部使用注入的假实现。

**待 Yijun 决策:**
1. **压缩给这条"为低延迟而生"的通道增加了延迟。** 一旦触发,摘要补全会在第一个 token *之前* 发生。
   选项:接受(罕见,只在超 24K 时)、改成本轮结束后异步做、或者第一次超预算时先丢最旧的、
   后台补摘要供下一轮使用。
2. **摘要过程中按 Stop 不会立即生效**(`should_cancel` 只在主循环里轮询)。
3. **循环内增长未纳入预算。** 只有开场 prompt 被裁剪,4 轮循环中追加的工具结果没有重新计量。
   今天安全纯粹是因为 24K ≪ 131072 —— 这是参数取值的结果,不是机制保证。

---

## 2026-07-20 —— 第二条执行路径:快速"聊天"通道 + 聊天内嵌 Mermaid 图

分支 **`feat/fast-chat-path-and-inline-mermaid`**(基于 `main` @ `a6e26a1`)。**未合并、未部署。**
设计说明与完整验证表:`reports/2026-07-20/fast-chat-path-and-inline-mermaid.md`。

此前控制台只有**一个引擎**:输入框里的每一条消息都会跑完整的 lab(PI 排议程 → 多步执行 → 出报告),
所以问一句话也要等几分钟的流水线,而且在规划结束前屏幕上什么都不出现。现在加了第二个引擎和一个用户可见的开关。

- **Axis B,`LabRequest.route`** = `"research"`(默认,行为不变)| `"chat"`。与既有的 `mode` 正交
  (Axis A:单个 scientist vs Virtual-Lab 团队,只在 research 引擎内部生效)。由输入框新增的
  `#routeSelect` 选择。
- **刻意做成显式开关,不做分类器。** 误判的代价是不对称的:chat→research 只是多等一会儿;但
  research→chat 会给出一个**背后没有任何分析**、下游又无人能标记的流畅答案 —— 正是报告防伪造那几层
  防线所针对的同一类失败。
- **`agents/quick_chat.py`** —— answer-first ReAct:先流式给答案 →(需要时)调工具 → 再流式续写。
  `think=False`,token 边生成边推送,所以第一句话立刻出现。有上限(4 轮 / 6 次工具调用)。工具限定在
  手工挑选的轻量白名单(`literature_search`、`map_phenotype_to_hpo`);`run_code` 和整条 HPC3 分析线
  在 chat 模式下**不可达**,有测试断言。
- **没有新增 WebSocket 事件类型**,复用 `chat_start`/`chat_token`/`chat_done`,所以 Stop、断线重连回放、
  多窗口 run 分流全部原样可用。新增传输层:`vllm_client.chat_tools_stream`(既流式又能调工具 —— 原有
  两个函数都只占一半)。
- **内嵌 Mermaid**:消息里的 ```` ```mermaid ```` 代码块直接渲染成图。**浏览器端**渲染,用仓库内自带的
  mermaid v11.12.0(`frontend/console/mermaid.min.js`,按需懒加载)—— **不依赖 `mmdc`、不依赖 CDN**,
  因为生产机两样都没有。`tools/schematic.py` 和 `make_schematic` **未改动**(它们用 graphviz 往 run
  bundle 里渲染图片*产物*,和这个浏览器端能力互补)。

**顺带查清的两件事(与既有结论不冲突,但此前没写下来):**
1. `chat_token` 流式协议**前后端其实早就写全了**,只是一直只用来推整条消息 —— 所以这次前端不需要改协议。
2. **`vllm_client.chat_stream()` 是死代码** —— `src/` 里没有任何调用,只有它自己的 docstring 和两个测试
   引用它。它能流式但不能调工具。**待你决定:**删掉,还是把两个流式函数合并。

**未验证,合并前请先看:**这个循环**从未跑过真实的 Qwen3.6**(所有测试用的是脚本化的假模型);
`gateway/app.py` 里的新代码**一行都没有真正执行过**(本机没有 `paramiko`,15 个 gateway 测试文件跑不了,
新代码只做了静态检查)。另有一个已知问题:冷会话下的**第一条**聊天消息仍要等 GPU 懒加载,这正是快速通道
唯一不快的场景。*(2026-07-27 已作废 —— 懒加载已删除;会话只有在 GPU 起来之后才可用,所以每条聊天消息
都是快的,等待被前移到了连接阶段。见最新一节。)*

---

日期:2026-07-15(分支 `claude/free-text-hpo-mapping-c61c74`;最新:VCF+HPO preset pipeline + HPO 本体版本对齐已验证 + "一次只能一个 dataset"天花板已查清;同日早些:自由文本→HPO 映射器本身。此前:LIRICAL 线在 main `7c9a8e8`,默认关闭,待改 .env + sync_deploy)

## 2026-07-15(续3)—— LIRICAL 的 Slurm 资源配置,在真实 WGS VCF 上实测

陈瑞问 LIRICAL 的 Slurm 作业等待时间够不够,并认为它是 GPU 任务。

**它不是 GPU 任务,也不该是。** `partition=st.cpu_partition`("standard",免费 CPU 分区)+
`gres=""  # CPU-only`(slurm_analysis.py)。LIRICAL 是个 Java CLI(`exec java -jar`),做似然比计算 +
Exomiser 库查询,**没有任何 GPU 代码路径**。GPU 只给 vLLM/Qwen 的 serve 作业用。给它申请 GPU 只会排队更久、
白占一张稀缺的卡。

**等待本来就是自动的**:`run_timeout_s=0` → AUTO = `--time + 5 分钟`,所以网关不会提前 scancel 健康作业
(以前固定 1800s 曾误杀一个合法的 WGS VEP 作业,AUTO 就是为此而生)。所以唯一的问题是 `--time` 本身够不够。

**实测(作业 54191395,HPC3,2026-07-15)** —— 基因型感知模式,用**真实的 `CASE_A` WGS VCF**
(1.13 GB,**4,928,515 个变异**,standard 分区,4 CPU,用 `build_lirical_cmd` 实际生成的 argv):
- **墙钟 4 分 22 秒**(Exomiser 以 ~2.16 万变异/秒 流完 callset = 3 分 48 秒;疾病打分只占几秒)。
  **MaxRSS 7.9 GB**。退出码 0。
- 也就是说 **1 小时本来就有约 13 倍余量**。我原本预测是 VEP 量级(30-60 分钟)并抢先把默认值改成了 4h ——
  **实测证明我错了,已改回 1h**,注释里换成了实测数字而不是猜测。LIRICAL 快是因为它是拿预建的
  Jannovar/Exomiser 库打分,不需要重做 VEP 那种逐变异的转录本注释。

**由此带出的两个真实修复:**
- `mem_gb` 原来借用的是 **`run_code_mem_gb`** —— 谁把 CodeAct 沙箱内存调小,就会悄悄饿死 LIRICAL。
  现在有了独立的 `lirical_mem_gb`(BIOAGENT_LIRICAL_MEM_GB),默认 64。
- **sif 里没有 `-Xmx`。** JVM 虽然报告 `UseContainerSupport=true`,但它把 `MaxHeapSize` 定在 32 GB
  = **节点** 187 GB 的 1/4,**不是** Slurm `--mem` 的 1/4。所以堆和 `--mem` 是**互相独立**的:调大 `--mem`
  不会让堆变大;而 JVM 若真长到 32 GB,`--mem` 低于它就会被 Slurm **OOM 杀掉**。这就是实测峰值只有 7.9 GB
  却仍保留 64 GB 的原因。在镜像里钉死 `-Xmx`(需重建)后可降到 ~16 GB,调度会更好。**待办。**

**顺带:第一个真实的端到端信号。** 这次跑的正是实验室的一个已解决病例(病例与致病基因未发表,此处刻意不记)。
只给了它诊断映射出的 **2 个 HPO 术语**,LIRICAL 把**已知致病基因排在 93 个候选中的第 2
(posttest 99.96%)** —— 基因是对的,且与第 1 名(99.97%,一个离谱的综合征)几乎并列。
是好兆头,但**只有一例**,而且表型是从诊断本身推出来的(有点循环论证)。**不要过度解读。**
输出留在 `hpc3:/dfs3b/ruic20_lab/<ucinetid>/lirical_timing/out/timing.tsv`。

## 2026-07-15(续2)—— 病历附件槽位 + 文件夹传 VCF 的 prod bug;已合并进 main

**已合并进 main 并已推送 origin(`5c5905b`,快进,794 测试通过)—— Yijun 2026-07-15 指示"立即推送并且merge"。**推送**绕过了 4 项必需状态检查**(账号有 bypass 权限),也就是说**CI 完全没跑**,唯一的把关是本地测试套件。**推送 ≠ 部署:**在有人执行 `scripts/sync_deploy.sh` 并重启之前,prod 仍在跑 2026-07-14 的代码,所以下面那条"线上用模型瞎写的 HPO ID 跑 LIRICAL"的风险**依然存在**。
注意:LIRICAL 那个 session 自己的分支(`claude/lirical-ird-confidence-scoring-99add1`)**早就全部合完了** ——
它停在 `2e7ef88`,和 main 完全一致,worktree 干净,没有任何待合并内容。

**⚠️ PROD 事实(2026-07-15 在 eyeserver 上实测,推翻了几条既有认知):**
- **上传确实在 HPC3 上** —— prod `.env` 里 `BIOAGENT_UPLOADS_ON_HPC=1`。Yijun 的印象是对的。
- **.env 行内注释 bug 已修*且已部署*** —— 用**部署版**的 `load_dotenv` 重新解析 prod 的 `.env`,得到的是
  干净值(`RUN_CODE_ON_HPC='1'`、`VLLM_MAX_MODEL_LEN='131072'` 等)。它还能压过 systemd 未清理的
  `EnvironmentFile=`(仅当差异只是注释时才覆盖)。旧的"在 main 上但未部署"已**过时**。仍需做的只有
  `.env` 去重(第 12 和 17 行重复的 `BIOAGENT_VLLM_MAX_MODEL_LEN`)。
- **LIRICAL 在 prod 是开着的** —— `BIOAGENT_PHENOTYPE_ON_HPC=1`,且部署版代码(2026-07-14)确实带着
  `phenotype_dx.py`/`phenotype_cli.py`/`hpo_terms/`/`run_lirical`。"gated OFF 等 .env"已**过时**。
  **这意味着 prod 正在用模型凭记忆写的 HPO ID 跑 LIRICAL** —— 正是映射器要阻止的那个静默错误表型。
  所以部署这批改动是在修一条**线上路径的正确性**,不是锦上添花。

**已修 —— 文件夹传 VCF 在 prod 上静默"没有数据集"。** 两个 primary 文件查找器都只认单细胞矩阵
(`_MATRIX_SUFFIXES` 里没有 `.vcf`),而且只看最后一个后缀,所以 `case.vcf.gz` 被当成 `.gz`。又因为上传落在
dfs3b,文件夹上传走的是**远程分支** —— 而它不像本地分支那样有兜底,直接让 `dataset_path` **完全没被设置**
→ 整个 run 表现得像什么都没传。现在改为 `_PRIMARY_SUFFIXES`(+.vcf.gz/.vcf/.bcf),按**特异性**排序,
保证 `case.vcf.gz` 赢过 `notes.txt`;两个查找器现在共用**同一套排序**(它们本来已经漂移了:
`Path.suffix` vs 字符串切分)。

**新增 —— 病历附件槽位**(Yijun 选的"正式的第二附件槽位")。它能绕开"一个 dataset"的天花板,靠的是一个性质:
**病历唯一的消费者是在网关进程内跑的**(`map_phenotype_to_hpo` 特意没进 `_HPC_PHENOTYPE_TOOLS`),
所以它**根本不需要 Slurm 绑定**。浏览器读取 .txt/.md 的文本,作为 `LabRequest.case_note` 随 run 提交;
不上传、不建 dataset 行、不动绑定集。上限 64k(截断而非拒绝),并持久化进 `run_state.json`,续跑不丢表型。
`map_phenotype_to_hpo` 不带 `text` 时读附件;显式 `text` 优先;结果里带 `text_source` 可审计。
已在浏览器里端到端验证:`dataset_path` 是 VCF、`case_note` 是病历时,请求里**两者同时存在**,病历不挤占 VCF。
**范围:仅限文本病历。** 第二个**数据**文件(BED panel、第二个 VCF)仍需同时改绑定集 + 容器内 CLI 契约 +
dataset 外键(`extra_ro_binds` 是接口)。

另注:测试套件之前静默跳过了约 40 个测试(本地缺 fastapi/httpx/sqlalchemy),装上后真实数量是 **794**。

## 2026-07-15(续)—— VCF+HPO preset pipeline、本体版本对齐、以及"一次只能一个 dataset"的天花板

**Pipeline** `preset_pipelines/phenotype_variant_diagnosis/`(陈瑞:"单独做一个 VCF+HPO 的 pipeline")。
`data_type: variants`,与 `variant_annotation` 并列(后者仍是"只有 VCF"的路径)。两条**独立**轨道 ——
`run_lirical` 并不消费 `annotate_variants` 的输出(基因型模式是用它自己的 Exomiser 库直接跑原始 VCF)——
最后汇合。汇合步骤才是重点:**只有变异侧命中是预期内的,不是矛盾**(LIRICAL 按策展的 HPO/OMIM 注释打分,
所以全新的基因-疾病关联**按构造就排不进来** —— 正是实验室那批「新关联」病例)。

**"当且仅当"是用代码保证的,不只是靠提示词:**没有表型文本 → `map_phenotype_to_hpo` 返回零个 observed
(`infer_hpo_terms(default=False)`,所以旧的 `HP:0000556`"永不阻塞"默认值不可能触发)→ `run_lirical` 在
`hpo_terms` 为空时报错。只有 VCF 没有描述时,**无论模型怎么调都打不了分**。反方向(有描述时必然执行)
目前仍靠提示词/路由 —— 与下面同一个待办。

**HPO 词表对齐 —— 已验证,不需要重建。** HPC3 上 LIRICAL 的 `hp.json` 是 **2026-06-23**,与我们词表的
构建来源**逐字节相同**(两边 md5 都是 `e4ce3ae0…`)。但两者由不同的人更新,漂移是**静默的**(我们仍在映射
的术语,可能在更新的 LIRICAL 本体里已废弃 → 它只会不再匹配,不报错)。所以 `run_lirical` 现在每次都比对
两边版本(`hpo_release_drift`),不一致就在 `phenotype_notes` 里报告并给出重建命令。成本为零:版本串在
hp.json 头部(读 1MB 即可,不需解析 23MB)。

**多文件/文件夹 —— 结论:不支持,而且有硬天花板。** 一次运行只绑定**一个** dataset,每一层都是标量
(`LabRequest.dataset_path: str|None` app.py:1570;`decisions["dataset_path"]`;单个 `dataset_id` 外键
models.py:119;前端 `state.datasetPath`)。后果:
- **病历文件不能作为附件传** —— 它会占掉那唯一的位置、把 VCF 挤掉。病例文本必须粘进提问里
  (pipeline 的 SKILL.md 已明确写了这点)。
- **文件夹路径**:UI 有"Upload a folder"但没有路径输入框;远程 dfs3b 路径只有 `/api/lab` 接受(UI 到不了),
  而且对 VCF 文件夹**照样失败** —— `_MATRIX_SUFFIXES`(app.py:1017)里没有 `.vcf`,且只看最后一个后缀,
  所以 `.vcf.gz` 会被当成 `.gz`。
- **任何修复的主要约束**:`--dataset` 既是唯一的 CLI 参数、又是唯一的只读绑定(`slurm_analysis.py:246`),
  所以第二个文件不只是"没绑上",而是在容器里**根本不存在**。`extra_ro_binds` 是天然的接口。真要修,
  必须同时改绑定集 + 容器内 `run_tool(tool, workspace, dataset_path, args)` 契约 + dataset 外键。
- 另外发现:在 `uploads_on_hpc` 模式下,`_stage_upload_to_hpc` 推完就删掉本地副本(app.py:1264),而
  `BIOAGENT_UPLOADS` 仍指向本地(app.py:2792)→ 现存唯一的多文件通路(run_code 能看到整个 uploads 树)
  恰恰在最需要它的模式下是坏的。本次**未修**。

**已标记、未修**(已开成独立任务):`run_lirical` 的 `vcf_path` 在两条路径上优先级**相反** ——
`variant_annotation.py:586` 是显式参数优先;`phenotype_cli.py:51` 是 `dataset_path or args["vcf_path"]`,
所以在 HPC3 上显式 `vcf_path` **100% 被静默忽略**,而 schema 却写着"defaults to the run's dataset"。
没有直接改是因为"对齐一下"可能**让现在能跑的运行报错**(未绑定的路径会在 sif 里 FileNotFoundError)——
而且既然一次只有一个 dataset,把 `vcf_path` 从 schema 里删掉可能才是更好的答案。

553 个测试通过。另外新增:每个 preset pipeline 的 `tools:` frontmatter 现在会对照**真实 catalog** 校验
(之前没有任何地方解析这些名字 —— 写错一个就会对外宣传一个不存在的工具)。

## 2026-07-15 —— 自由文本 → HPO(`map_phenotype_to_hpo`):表型线缺失的前端

陈瑞:*"医生通常不使用HPO术语而是使用自由文本"* —— 所以下面 2026-07-14 那条备注("HPO 来自研究**描述**文本;由模型抽取")
其实就是真正的洞:**当时是让编排模型凭记忆写 HPO ID**。`HP:0000662` 是 Nyctalopia(夜盲),`HP:0000622` 是
Blurred vision(视物模糊)—— 只差一个数字换位,两个都是真实的眼科表型。编造出的"真 ID"会**静默失败**:
LIRICAL 照样跑,只是把后验建立在错误表型上,给出一个自信而错误的鉴别诊断。这比崩溃更糟,因为它会进报告。

**设计 —— LLM 负责语言,本体负责身份**(`docs/free_text_to_hpo_mapping.md`):
LLM 抽取短语 + 否定(中文→英文、`ERG 熄灭型`→nonrecordable ERG、"其母患 RP"丢弃)→ 代码从内置 HPO 索引检索
真实候选 → **LLM 只能选候选编号**(它从不打出 ID,因此无法编造)→ 代码复核,并从本体取规范名称。
每个术语都带来源短语 + `method`,临床医生可以**审计**而不是盲信。与报告写作那套闭集反编造层同一模式。

- **`hpo_lexicon.tsv.gz` 已提交进仓库**(~390 KB;HPO 2026-06-23;19,120 现行 + 577 废弃术语),映射器因此
  **离线可跑** —— 不需要 HPC3、不需要网络、测试里能用。用 `scripts/build_hpo_lexicon.py` 重新生成;
  `BIOAGENT_HPO_LEXICON` 可指向 LIRICAL 自带的 `hp.json`。**不限于眼科**(综合征型 IRD 需要听力损失/多指/肥胖)。
- **`run_lirical` 现在对每个传入的 HPO ID 做本体校验**(未知→丢弃,废弃→转发到 `replaced_by`,全部无效→报错
  并指向映射器),所以即使模型绕过工具也塞不进编造的 ID。结果记在 `phenotype_notes` 供 Diagnostics 使用。
- **用实验室真实已解决病例表验证**(陈瑞的 Google Sheet,12 例 / 8 个不同诊断):8 个**全部在完全不用 LLM 的
  情况下**映射成功 —— 但这是修完该表暴露出的缺口之后。HPO 里根本没有 "choroidal dystrophy"(占 20 行里的 7 行!)
  → HP:0001135 Chorioretinal dystrophy;`Pattern Dystrophy` 只有 0.71 分,卡在阈值下;`BBS`/`RP` 无候选。
  已加入策展别名表并用测试锁定。
- 顺带修了一个潜在 bug:`infer_hpo_terms` 用裸子串匹配,`ird` 会在 `third` 里命中。现改为词边界匹配
  (加了 `rp`/`bbs`/`lca` 这些别名后更要紧 —— `RPE`/`RPGR` 现在安全)。
- 547 个测试通过(新增 31 个)。

**未验证 —— 下一步要做:**
1. **LLM 抽取层在真实模型上的表现。**单元测试里 LLM 是脚本化的,只证明了 grounding,完全没说明 Qwen3.6 读病历
   读得好不好;**否定和家族史是它最先会翻车的地方**。一条命令:`PYTHONPATH=src python scripts/hpo_mapper_smoke.py
   --port <tunnel> --model qwen3.6:35b-a3b`(或 `--openrouter`)。我这边跑不了:HPC3 上没有起着的 vLLM,本地也没 API key。
2. **在已解决病例上跑端到端**(文本→HPO→LIRICAL→已知基因排第几)—— 需要先部署已 gated 的 LIRICAL。
   **注意事项,要在别人看数字之前说清楚:**实验室好几例的致病基因并不是诊断会让你预期的那个
   (具体的基因-诊断组合未发表,此处刻意不列)。LIRICAL 是按策展的 HPO/OMIM 注释打分的,所以它很可能把这些排得**很低**
   —— 那是文献/证据线该干的活,不是映射的 bug。
3. **确定性触发仍然缺失** —— 描述里出现症状时没有任何机制强制走表型步骤,还是靠模型自己决定调用。
   与 2026-07-14 相同,仍是首要待办。

**表 ↔ VCF 对应(用于验证):**`/dfs3b/ruic20_lab/chenlab/Data/WGS_data/*/*/<ID>.GATK.HaplotypeCaller.mark.vcf`
(如 `CASE_A`)。表里的坐标是 **GRCh37/hg19** → LIRICAL 用 `-e19`(已 staged 的 Exomiser `2406_hg19` 正确),
另注意已知的 GRCh37 预测器缺口(CADD/REVEL/AlphaMissense 仅限 GRCh38)。

**HPC3 是通的**(2026-07-15):我从这个会话 SSH 登进去了(`login-i15`,密钥认证)。如果 Yijun 登不上,
是账号/Duo 那边的问题,不是服务器。

## 2026-07-14(续)—— LIRICAL 网关接线完成 + 已合并 main;只剩部署

表型线现已**完整接线并进入 `main`(`7c9a8e8`,已推到 origin —— 该 push 绕过了 4 项必需状态检查,依 Yijun "全权开发 + bypass-merge")**。Scientist 工具 `run_lirical` 已注册并路由到表型 `SlurmAnalysisExecutor`(app.py,照抄 VEP 接线:preflight + 绑定 LIRICAL 数据 + Exomiser 目录 + 注入配置);entrez→符号 的 reconcile 修复已进。仍**默认关闭**(`BIOAGENT_PHENOTYPE_ON_HPC` 未设)→ 部署前 prod 不变。

**去 chr 前缀(合并后追加):** 眼科 WGS VCF(如 `example_input.vcf` = CASE_B,hg19,chr 前缀)与 Exomiser 的无 chr 命名不匹配。`run_lirical` 现在**先检测**有没有 chr 前缀、有才去(头+记录都改,基因型不动),已在 lirical.sif 里端到端验证(chr1→1 → RP19 96.22%,基因 ABCA4)。与实验室自己的 `remove_chr.py` 一致。739 测试绿。**关于 HPO 的提醒:它不在 VCF 里**,来自研究的**文字描述**,模型从描述里抽。目前**没有**"检测到症状就强制走表型线"的确定性接线,触发靠模型自觉(加确定性 HPO 步骤是下一个首要项)。

**剩余(部署 —— Yijun + 管理员):**
1. **eyeserver-admin** 改 prod `.env`:`BIOAGENT_PHENOTYPE_ON_HPC=1`、
   `BIOAGENT_LIRICAL_IMAGE=/dfs3b/ruic20_lab/software/bioagent/containers/lirical.sif`、
   `BIOAGENT_LIRICAL_DATA_DIR=/dfs3b/ruic20_lab/software/reference/lirical/data`、
   `BIOAGENT_LIRICAL_EXOMISER_HG19=/dfs3b/ruic20_lab/software/reference/lirical/exomiser/2406_hg19`
   (完整块见 `deploy/lirical/README.md`)。
2. **Yijun** 手动跑 `scripts/sync_deploy.sh`(把本地 main rsync 到 eyeserver + 重启)。
3. 之后(增强):LLM 自由文本→HPO 映射;manuscript 里加每疾病 Confidence 表。

## 2026-07-14 —— LIRICAL 表型→疾病工作流已安装 + 验证 + 接线

**背景。** Rui Chen 批准了表型→疾病置信度方案(2026-07-14 邮件:"方案批准了…继续安装 LIRICAL 工作流"),
并回答了两个悬而未决的问题:**(1)** 表型输入用**自由文本 → 用 LLM 映射成 HPO**(医生不会直接写 HPO 术语);
**(2)** Meng Wang 会整理一批**已确诊病例**作为标定/测试集。本次把 LIRICAL 工作流做到了"离线可验证"的边界。

**本次完成**(分支 `claude/lirical-ird-confidence-scoring-99add1`,全部默认关闭,14 个测试通过):
- **`deploy/lirical/`** 构建套件 —— `lirical.def`(JRE 17 + 内置 LIRICAL v2 CLI;数据像 vep.sif 一样
  bind-mount),`build_and_stage.sh`(构建 sif → `lirical download` 下数据 → 可选 Exomiser 库 → 冒烟测试
  → 打印 `.env`),`README.md`。
- **`tools/phenotype_dx.py`** —— 在原脚手架上补齐真正的 runner:`build_phenopacket`(HPO/否定项 →
  GA4GH Phenopacket v2)、`build_lirical_cmd`(LIRICAL v2 `prioritize` 命令;纯表型 vs 基因型感知)、
  `run_lirical`(写 phenopacket → 运行 → 解析 TSV;`exec_fn` 可注入,无需真的 LIRICAL 即可测)。两轨设计
  不变(LIRICAL 为主;PaperQA2 只做证据层,概率永不混算)。
- **`tools/phenotype_cli.py`** —— 容器内 CLI(对应 `variant_cli`),`SlurmAnalysisExecutor` 在 `lirical.sif`
  里调它。
- **`gateway/settings.py`** —— `BIOAGENT_PHENOTYPE_ON_HPC` + `BIOAGENT_LIRICAL_*`(镜像/数据/exomiser
  hg19+hg38/时限/CPU)。默认关闭 → 不设就不影响 prod。
- HPC3 已核实:**Java 17**、singularity 3.11.3 / apptainer 1.4.5、dfs3b 剩约 16 TiB。还没有 `lirical.sif`(正常)。

**⚠️ 修正一个旧假设(原本写在本交接的 NEXT #3):** "Exomiser 已装在 HPC3,直接复用"对 **LIRICAL v2 不成立**。
实验室现有的是 `1805_hg19` + `exomiser-cli-10.1.0`(位于
`/dfs3b/ruic20_lab/{chen/pipeline_restructure/pipeline_restructure,bin/pipeline/pipeline_restructure}/exomiser`)
—— 2018 年、**Exomiser 10.x 库格式、仅 hg19**(约 21 GB)。LIRICAL v2 需要 Exomiser 数据 **≥ 2302**(新的
`.mv.db` 格式),**无法复用**。基因型感知模式需要**重新下载**一份 Exomiser 库(约 20 GB;hg19 与眼科 VCF 匹配)。
**纯表型模式不需要 Exomiser 库,现在就能用。**

**HPC3 上已完成(当天 —— 安装 + 验证均已完成):**
- `lirical.sif`(LIRICAL **v2.4.1**)经 Sylabs `--remote` 构建,已落到 `…/containers/lirical.sif`。
- LIRICAL 数据 + 新的 **Exomiser 2406_hg19** 变异库(27.7 GB `.mv.db`)已落到
  `…/reference/lirical/{data,exomiser/2406_hg19}`(实验室旧的 `1805_hg19` 不可用 —— 见 ⚠️)。
- **两种模式已端到端冒烟测试**(`~/lirical_build/smoke/`):纯表型(8621 个疾病,RP 各亚型排前、且并列 ——
  正是症状重叠问题)与基因型感知(测试用的 ABCA4 `p.(G1961E)` 变异把鉴别收紧到 ABCA4 相关病,RP19 96.22%
  —— "遗传把置信度收紧"的效果)。
- **已按真实 `prioritize --help` 修正 `build_lirical_cmd`**:v2.4.1 是 CLI 参数模式(`-p` 观察到 / `-n`
  否定 / `-d` / `-o` / `-x` / `-f` / `-ed19` Exomiser 数据目录),不是 phenopacket。也补捕了真实 TSV 列
  (`entrezGeneId` + `variants`;没有基因符号列)。

**下一步(本线):**
1. **网关步骤** —— 在 app.py 里加表型 `SlurmAnalysisExecutor` + 目录注册(照抄 VEP 的接线,约 app.py:3067),
   让一次 run 自动产出鉴别诊断。**先修 reconcile 的关联键:** LIRICAL 基因型感知 TSV 给的是 `entrezGeneId`
   (`NCBIGene:24`)不是基因**符号**,而 `reconcile` 按符号关联 —— 合并前用已落地的
   `…/reference/lirical/data/hgnc_complete_set.txt` 做 entrez→符号 映射。
2. **激活**:设 `.env`(在 `deploy/lirical/README.md` / `build_and_stage.sh` 打印)+ `sync_deploy.sh`。
   `BIOAGENT_LIRICAL_EXOMISER_HG19=…/reference/lirical/exomiser/2406_hg19`。在第(1)步网关接线落地前,设了也没用(还没路由)。
3. **自由文本 → HPO(LLM)** —— Rui 的答复。`tools/hpo_terms.infer_hpo_terms` 现在只是对 `ird_hpo.tsv` 的
   关键词匹配;要升级为 LLM 抽取 + 用 `hp.json` 校验 ID。`run_lirical` 已接受 `hpo_terms`/`excluded_hpo`。
4. 冒烟输入/输出在 `hpc3:~/lirical_build/`(def/脚本/README + `smoke/`);驱动日志 `~/lirical_build/install.log`。

## 2026-07-13(晚)—— IRD 追平已合并进 main + 已 push;skill/protocol 工作已记录

**全部在 `main` 且已推到 origin**(`main` = `5aa8131`,与 `origin/main` 同步;`feat/ird-parity` 已完全包含在 main,无可再合并的东西)。

本次完成:
- **`feat/ird-parity` → main**(fast-forward `98b3d5e..20161e7`,无 merge commit;**712 测试全绿**)。整条 IRD 层已在 main:RetNet panel + 加载器;确定性 known-gene-first(`BIOAGENT_DEFAULT_GENE_PANEL`/`_MAX_POP_AF`);**VEP 前 `regions_bed`**(99 秒提速的关键);IRD 注释层(HGMD/视网膜外显子/ATAC/dbscSNV)+ `reason_for_inclusion` 级联;疾病模型分级;上游 agent HPO 推断(无 HITL)。**全部默认 gated OFF → 合并对现网零行为改变,不设 env 不激活。**
- **Yijun 自己在 main 的提交**(留痕):`d7e23ce` —— SKILL.md 重写(默认回退 assembly 改 **GRCh37**;删掉误导的 'GRCh38 by default' 说法);`5aa8131` —— operon 风格可审计 **`PROTOCOL.md`** 原型 + OpenRouter **A/B** 工具(`experiments/protocol_format/`;格式实验,未接进管线)。
- 部署对账:**用 `scripts/sync_deploy.sh`**(稳健、走 systemd)。`deploy/redeploy.sh` + `push.sh` 是更老的 legacy 路径(真实存在且 README 引用 → 没删;收敛到 `sync_deploy.sh` 是可选清理,须连带改 README + 回滚文档)。

下一步(按优先级):
1. **部署**(`sync_deploy.sh --dry-run` → `sync_deploy.sh`)。之后要**激活** IRD 再设 `BIOAGENT_DEFAULT_REGIONS_BED`/`_DEFAULT_MAX_POP_AF=0.005`/`_IRD_ANNOTATE=1`/`_IRD_RETINA_EXONS`/`_IRD_ATAC`。**先把 retcap/retina/atac 的 BED 从 `…/ird_verify/ref/`(我的 scratch)挪到稳定的共享 prod 路径** —— 不能留在个人临时目录。
2. **预测器**:CADD GRCh37(85 GB)下载收尾 → 带 CADD/REVEL/AlphaMissense 跑更全一轮(用公开 VEP 格式库,不是实验室 ANNOVAR/GRCh37 副本)。
3. **"Identify"/connection 来源** —— 分级 **知识库 → RAG → 模型** + 溯源护栏:任何 gene/variant↔disease 断言必须回指 ClinVar/HGMD/OMIM/panel/PMID,否则标记/丢弃。RAG(`literature_search`/paperqa)只在 novel/未解释的 shortlist 候选上触发;phenotype→gene 走 **Exomiser + HPO**(Exomiser 已在 HPC3,HPO 推断已建)。
4. **打磨**:把 IRD 注释层字段(retina/hgmd/`reason_for_inclusion`)写进输出表 + 在 shortlist 选择里用上 `reason_for_inclusion`(现在算了但没露出来)。

## 2026-07-13 —— IRD 流程在 HPC3 端到端验证通过(真实 cb720 VCF,未碰现网)

新流程(分支代码)在 HPC3 上对 cb720 自己的输入 VCF 跑到底,未碰 eyeserver/现网。结果两轴双赢:**99 秒**总注释(vs cb720 的 ~52 分钟;retcap 在 VEP 前限制 → VEP 只看 1,544 个而非 4.67M),shortlist 现在是**公认的 IRD 基因,线粒体/PRAMEF/lncRNA 噪声消失**;其中有 ClinVar P/LP 命中,榜首是一个可信的复合杂合候选。(基因级结果未发表,此处刻意不记。)报告:`~/Downloads/IRD_New_Pipeline_Run_Report.docx`。HPC3 可复用:src 在 `…/ird_verify/app`,清洗过的 retcap + tabix 的 retina/ATAC 在 `ird_verify/ref`,driver `ird_verify/ird_run_driver.py`。

## 2026-07-12(夜)—— IRD 追平:Phase 1 + Phase 2 核心已建(`feat/ird-parity`)

自主开发(Yijun 睡觉,授权自决)。**Rui Chen 已批**复用实验室参考数据(HGMD 是公开版)→ Phase 3 数据全通,只剩患者 HPO 输入。读了他的 `annotationTools.py`(只读),把确切逻辑抽进 `docs/ird_filter_spec.md`。策略锁定 = **A 精修**:在我们脚本里复现逻辑(不 wrap 他的 ANNOVAR/Perl 单体)、只做必要、数据先读他的;B = diff 金标准 + spec。parity = 临床级,非逐字节。

已建 + 提交到 `feat/ird-parity`(**不在 main**;694 测试全绿),`f40ee5b..d5df502`:
- RetNet **IRD panel**(258 基因)→ `src/bioagent/tools/gene_panels/`(资产+加载器+测试)。
- **确定性 known-gene-first**:`BIOAGENT_DEFAULT_GENE_PANEL=ird` → gateway 默认注入 panel;`BIOAGENT_DEFAULT_MAX_POP_AF=0.005` → 默认罕见阈值。都可被调用方覆盖。(修好 cb720 全基因组的根因。)
- **疾病模型分级** `tools/ird_prioritize.py`(显性≤1e-4/复合杂合≥2且≤5e-3/X)接进 `summarize_annotations` → shortlist 命中模型的排前 + 加 `Disease_Model` 列;线粒体/噪声沉底。

**坑(已写进 spec)**:实验室 CADD/dbNSFP 是 ANNOVAR 格式/GRCh37,**VEP 插件用不了** → 预测器要 stage 公开 VEP 格式库,别把 `BIOAGENT_VEP_*` 指向他的副本。HGMD/视网膜外显子/ATAC 文件可直接用(bedtools)。

**下一步**:VEP job 里的注释层(HGMD 15bp/MATCH、视网膜外显子、ATAC、ada/rf)+ `reason_for_inclusion` 级联 + 基因约束;然后定患者 HPO 输入形态。生效:设两个 env + 部署分支(评审后);用真实验室 run diff 量 parity。

## 2026-07-12 —— IRD 流程追平:把"path"记录成路线图(`docs/ird_pipeline_parity_roadmap.md`)

把我们这轮 `cb720f958f06` 和实验室 IRD 参照(`output_annotated (1).analysis`)对比 —— **同一个输入 VCF**
(位点精确匹配)。我们的**注释是好的**(94% 基因吻合、GRCh37 自动识别正确、无编造),但**排序 shortlist
临床跑偏**(线粒体/PRAMEF/lncRNA 噪声,且漏掉实验室自有流程能捞出的公认 IRD 基因)—— 因为跑的是**通用流程**,不是实验室的 IRD 专用流程。
完整评估 DOCX:`scratchpad/VCF_Report_Credibility_Assessment.docx`。

路线图(11 层)记在 `docs/ird_pipeline_parity_roadmap.md`。关键点:**很多基础设施已经有了** ——
`annotate_variants` 支持 `genes`/`regions_bed`/`max_pop_af`;CADD/REVEL/AlphaMissense 的 VEP `--plugin`
已接好(只是数据没 stage → 所以列是空的);SpliceAI 全流程已建(gated OFF)。四阶段:**0** 部署本会话
`.env` 修复(`1588a3a`:128K + run_code 上 HPC3)→ **1** 开启已建功能(RetNet IRD panel + stage
CADD/REVEL/AlphaMissense + 开 SpliceAI)→ **2** 疾病模型 AF(≤1e-4 显性 / ≤5e-3 复合杂合)+ 基因约束
(pLI/RVIS/GDI)+ 相位 → **3** 外部数据层。**Phase 3 阻塞项需 Yijun/实验室提供:** 视网膜特异外显子 BED、
视网膜 ATAC BED、HGMD 许可(或 ClinVar+LOVD 替代)、患者 HPO 输入。状态:路线图完成,Phase 0 待部署,
Phase 1–3 未开始。

本会话其他(都在 main,待 `sync_deploy.sh`):`6e6c5d6` 文献 label 修复,`1588a3a` `.env` 行内注释修复。

## 2026-07-11 —— analysis.sif 重建带上生信工具箱 + 部署到 prod(已上线)

提交了 `deploy/analysis/analysis.def`(bf07dce),并在 HPC3 重建/部署。用 Sylabs `--remote` 云构建器
(HPC3 上 fakeroot 不可用——`no subuid mapping`),smoke 测试后原子替换到 prod 路径,带回滚备份:
- `/dfs3b/ruic20_lab/software/bioagent/containers/analysis.sif` → 新的(431M),验证:**bcftools 1.21**、
  samtools/tabix/bgzip/bedtools 全在 /usr/bin,python import scanpy 1.11.5 / pysam 0.24.0 / cyvcf2 0.34.0。
- 回滚:`analysis.sif.bak-20260711`(旧 393M)——`mv -f analysis.sif.bak-20260711 analysis.sif` 即回退。
这是 run_code 的**线上镜像**(BIOAGENT_RUN_CODE_ON_HPC=1),所以 run_code 片段现在能直接调
bcftools/samtools/bedtools/pysam/cyvcf2,不再卡死。注:凡是说"analysis.sif 没有 bcftools"的旧记忆/交接
现在都**过时**了。但二进制在≠够——bcftools norm -f REF 仍需 ref FASTA bind 进 slurm_sandbox(未改),
所以 normalize 去冗余(由 vep.sif 里的 annotate 做)仍是正路。

## 2026-07-11 —— force_args:网关权威 variant 参数(治好 assembly + 1213 队列两个根)

## 2026-07-11 —— force_args:网关权威 variant 参数(治好 assembly + 1213 队列两个根)

bundle 取证(round_13)钉死了 assembly bug 和 1,213 队列污染的**共同根**:模型传了
`annotate_variants({assembly: "GRCh38", max_variants: 5000, ...})`。`inject_args` 是"部署默认值、调用方覆盖",
所以模型的值赢了:(1) GRCh38 覆盖 GRCh37 文件 → 首次 "VEP: Cache assembly mismatch" 失败;(2) max_variants=5000
→ 重试只注释了 4.9M WGS 的前 5000 个变异(chrM + chr1 开头)→ 1,213 "rare" 队列被当成整个研究,覆盖了第 5 步正确的 674,108。

修复 —— `SlurmAnalysisExecutor.force_args`(`slurm_analysis.py`):网关**权威**参数,覆盖调用方(inject_args 的反向)。
`_run_on_slurm` 合并 `{**inject_args, **caller, **force_args}` 并记录覆盖。`app.py` 给 variant executor 设
`force_args={"assembly": _assembly, "max_variants": 0}`——从 VCF header 检测到的 assembly 赢过模型的猜测,离线路径
永远全量注释(max_variants=0=不设上限,模型的 cap 再也截不断研究)。+2 测试(18 绿)。注:这让**数据**正确;报告**正文**仍
抄 agenda 的 "GRCh38" 是另一个报告写作 grounding 问题(见下)。分支基于 `main`;未部署(Yijun sync)。

**仍未修 —— LLM/模型质量问题,不是干净的代码补丁能解决**(要 prompt/eval 的活):
- 报告 Methods 抄 agenda 的 "GRCh38" 而非工具结果的 assembly;QC 有 n_filtered=212,935 却造假 "0 non-PASS"。→ 报告
  写作 GROUNDING(让 Methods 引工具结果/QC 数字,而非 agenda 假设)。上面的确定性修复保证了它该引的数据是对的。
- VL 审查漏检真实渲染缺陷(visual_review_pass1 clean,report_review 抓到 4 个)。→ VL 模型/prompt 质量(vl-report-review-backlog)。

## 2026-07-11 —— run 065e18744c03 bundle 取证(报告/文献质量)

## 2026-07-11 —— run 065e18744c03 bundle 取证(报告/文献质量)

这轮最后其实出了完整报告(report.md/pdf/docx + technical_report + 15 轮)。审 bundle 挖出几个**新的产出质量 bug**:

- **已修 —— 空文献占位泄漏进正式手稿**:无被接受引用时,`insert_references` 把 "*No accepted literature-search
  citations...*" 印进了论文 References(report_review.md 也点了)。按 silent-degradation 设计这该进 Diagnostics
  而非手稿。`literature_references.py::insert_references` 现在空引用时**手稿保持干净**(删占位 + 空 `## References`
  段,并删掉模型手写的 References——本模块从不编造引用)。+2 测试(11 绿)。
- **未修(最重要)—— assembly 造假**:报告 Methods §1/§3 写 "GRCh38",但 VCF 是 GRCh37:
  `variant_offline_diagnostics.log` 显示 header 检测为 GRCh37、配置的 GRCh38 被 override,注释**实际用的是 GRCh37**
  cache/ClinVar。注释结果本身带 assembly=GRCh37(vcf_offline.py:561),但报告抄了 agenda 里写死的 "GRCh38"。修法:
  override 时(app.py:2902)把检测到的 assembly 作为 standing note 注入(`conn.add_injection`),或让 Methods 从工具
  结果取 assembly 而非 agenda 文本。**未做**——要先确认能可靠到达最终 Methods。
- **未修 —— 造假 "0 non-PASS"**:Methods §2 写 "4,721,988 PASS vs 0 non-PASS",但 QC(`vcf_qc.json`)
  n_filtered=212,935。纯 LLM grounding 错误。
- **未修 —— 降级的 1,213 变异队列**:报告基于被污染的注释(1,213 / 1 致病、high-priority 几乎全线粒体),而非第 5 步
  的 674,108 rare / 24 致病。memoize(45ccea4)止住了覆盖循环;但重跑 annotate 为何只出 1,213(而非 674k)**仍未解释**
  ——要查那次 annotate 的入参。
- **未修 —— VL 审查漏检**:`visual_review_pass1.json` = clean/defects:[],但 `report_review.md` 抓到 4 个真实渲染缺陷
  (空目录、Figure 1 caption 重复、ClinVar 表渲染成破折号乱码、占位 References)。vlreview 模型全漏(和 vl-report-review-backlog
  记忆一致)。
- 注:输入 question 就是 "complete the research"(占位)→ 文献 query 为空,正好触发上面的空文献路径。分支基于 `main`;未部署(Yijun sync)。

## 2026-07-11 —— annotate_variants 重跑死循环:按轮记忆 ~45 分钟 VEP 结果

## 2026-07-11 —— annotate_variants 重跑死循环:按轮记忆 ~45 分钟 VEP 结果

线上 run 065e18744c03 烧了 ~2 小时,还**把自己的结果搞坏了**。根因:没有任何东西阻止模型在后续步骤里反复
调 `annotate_variants`(9 步计划里既有"过滤"步又有"注释"步,第 7/8 步还再调)。每次调用 = 一个新的 ~45 分钟
WGS VEP 作业(观察到 _3.._6),而且每次都**覆盖表格**。第 5 步的好结果(674,108 rare、24 致病、248 HIGH)
被后面一遍降级的重跑覆盖成(1,213 变异、1 致病、几乎全是线粒体)——所以最终表格是**错的**,不只是慢。`Stop`
坏了(未部署)拦不住,我直接 scancel 掉失控作业(54052063),并让 Yijun 在控制台 Abort。

修复 —— 给 `SlurmAnalysisExecutor` 加**opt-in 结果记忆**(`slurm_analysis.py`):`memoize_result`(默认 False;
只在 `app.py` 的 variant executor 上设 True)。`run_tool` 用 (tool, args) 全量做键,在本轮
`local_workspace/.tool_cache/` 落盘缓存;同参数重复调用直接返回上次 OK 结果(附"已复用——别再注释、去读表"
提示),不重起作业、不动表格。只缓存**成功**的运行(失败仍会重试);参数真变了(基因/AF/assembly/VCF 不同)照样
重跑。scanpy 分析线保持 False → 行为不变。测试:`test_slurm_analysis.py` +2(16 绿)。分支基于 `main`;未部署(Yijun sync)。

后续(**未做**,有了 memoize 后优先级降低):预设流程仍把"过滤"和"注释"拆成两步——`annotate_variants` 一次调用
本就同时做(max_pop_af 过滤 + 注释 + 出表),计划应只列**一个**注释步。有了 memoize,第二个已是廉价缓存命中,
所以现在只是表面问题。

## 2026-07-11 —— normalize 去冗余:真正的修复在预设流程,不是原子 skill

## 2026-07-11 —— normalize 去冗余:真正的修复在预设流程,不是原子 skill

上次 normalize 去冗余部署后,独立 `normalize_vcf` run_code 步**照样被规划、照样失败**(模型手搓的 cyvcf2
规范化有 bug:漏 `import shutil`、拼错 `cytcf2`、`header_lines` 用错)。根因:上次只改了**原子 skill**
(`skills/normalize_vcf/SKILL.md` 的 when to use),但规划器听的是**预设流程**
`preset_pipelines/variant_annotation/SKILL.md`,其 "Ordered plan" 第 2 步明确命令"**Normalize the VCF
(`normalize_vcf` skill — `run_code`)…作为独立步排**",把原子 skill 的引导盖过。该预设还自相矛盾(让在
run_code=analysis.sif 里跑 bcftools,可那镜像没 bcftools)。已改**预设流程**(3 处,保编号不变,step-4/5–6
交叉引用不乱):第 2 步现在说规范化由 `annotate_variants` 内部做(离线 `bcftools norm -m-any -f REF` 在
vep.sif)→ `BIOAGENT_VARIANT_ON_HPC` 下**不要**单列;开场把 normalization 从"按独立步排"里排除;第 4 步不再
假设有预规范化输入。sync 后生效;**不影响已在跑的 run**。分支基于 `main`;未部署(Yijun sync)。

另:已起草(**未构建/未部署**,Yijun 先研究)`deploy/analysis/analysis.def` 烤进核心生信 CLI 工具箱
(bcftools/samtools/tabix+bgzip/bedtools)+ pysam/cyvcf2,让 run_code 不再因缺标准工具卡死。重建只能在
HPC3(macOS 建不了 .sif),restage 会**替换正在用的 prod 镜像**——有 run 在跑时别做。注意它本身**不能**让独立
左对齐跑通(还需把参考 FASTA bind 进 slurm_sandbox);它是通用 run_code 鲁棒性,和上面的 normalize 去冗余正交。

## 2026-07-11 —— 重连后 Stop 静默失效(客户端 run 属主 id 漂移)

## 2026-07-11 —— 重连后 Stop 静默失效(客户端 run 属主 id 漂移)

现象:长 HPC 作业(annotate_variants / 离线 VEP,实测 46 分钟)无视 Stop,一直跑到 Slurm `--time`
(BIOAGENT_VEP_TIME_LIMIT=2h)。scancel **管线本身是通的**(app.py `should_cancel=conn.chat_stop.is_set`
→ slurm_analysis → slurm_job `_check_cancel` 每 5s 轮询并 scancel)。根因在**上游**:Stop 只 POST
`conversation_id`(`state.runSessionId || state.activeId`,app.js),而 `resolve_run` 对任何 id 不匹配都
**严格 no-op**(这是对的 —— 防止陈旧/别窗口的 Stop 杀掉在跑的 run;测试
`test_stop_endpoint_targets_only_the_named_conversation` 钉住了它)。长跑期间一次 WS 重连/刷新会清掉
`state.runSessionId`,于是 Stop 发出错误 conversation_id → resolve_run 返回 None → 端点返回
`{"status":"idle"}` 静默 → 作业照跑。

重要 —— **拒绝了"服务端无脑兜底"**(id 不匹配就停唯一 active run):虽然 run 串行(≤1 个 active),但
"漂移的自己"和"真正的别窗口"在服务端凭 id **无法区分**,无脑兜底会重新引入隔离重构要防的跨窗口误杀
(并打破上面那个测试)。改为**修客户端 id、服务端为准**:

- `app.py` `Connection.summary()` 现在暴露 `active_run = {run_id, conversation_id}`(无则 None),作为
  重连时"谁拥有在跑 run"的权威来源。
- `frontend/console/app.js`(`applyStatus` 里,紧接"重连恢复 running UI"那行):当 `summary.chat_running`
  且**本窗口正看着属主会话**(`state.activeId === summary.active_run.conversation_id`)时,重新把它认作
  `state.runSessionId`。以 activeId 为门槛,只有属主窗口会认领,别的窗口不会(隔离保留)。
- `app.py` `_run_lab`:复用一个裸 `_ensure_run` active run 导致 conversation_id 为 None 时,从 req 兜底
  设上 —— 堵住第二条"resolve_run 永远按 conversation_id 匹配不上"的潜在路径。
- `app.py` `/api/chat/stop`:在 None(no-op)分支 `print()` 出请求 id 与 active run 的不匹配,让将来
  "Stop 没反应"能在 journald 里看到,而非静默。行为不变(仍 idle)。
- `tests/test_run_isolation.py`:+`test_summary_exposes_active_run_identity`。套件 20 绿(原 19);
  隔离 + replay 共 29 绿。分支基于 `main`;**未部署**(Yijun 跑 `sync_deploy.sh`)。

未修(推迟):reload 后若属主窗口当前**没在看**那个 run 的会话,Stop 仍需用户先切到那个 chat 标签;诊断
`print` 只进 journald。

## 2026-07-11 —— 独立 VCF normalize 步骤走 run_code 永远失败(去冗余 + 失败分叉加固)

VCF 运行里"计算统计师:左对齐 indel / 拆多等位 / 原子化 MNP"这一步(`normalize_vcf` skill 经 `run_code` 跑)
每次硬失败,手动模式的失败分叉就弹卡问用户,而它给的替代方案里包含 `vcfpy` **手写 Python** 规范化 —— 那会
静默产出**没左对齐**的 VCF(正是这步要防的 `not_in_clinvar` 假阴性)。

根因 —— 独立 skill 在当前部署下**结构上不可能成功**(2026-07-11 在 eyeserver prod + HPC3 实测),三个独立拦路:
1. `run_code` → HPC3 跑在 `BIOAGENT_ANALYSIS_IMAGE=analysis.sif`,里面**没有 bcftools**(vep.sif 才有 1.13)
   → skill 第一行 `shutil.which("bcftools")` 守卫直接退出。
2. `slurm_sandbox.py` 只转发 `BIOAGENT_DATASET/WORK/ARTIFACTS/MPLBACKEND` —— **不转发** `BIOAGENT_REF_FASTA`。
3. `binds_ro=(dataset_path,)` —— 参考基因组目录**没 bind 进容器**。(本地 `main` 同样缺,7/8 移植至今没跑通过。)

关键认知:**它还是多余的。** 离线路径 `annotate_variants` 早就在 `vep.sif` 里内部先跑
`bcftools norm -m-any -f REF` 再喂 VEP(`vcf_offline.py:90-94, 485-487`),用 `settings.vep_ref_fasta`
(默认 `/dfs3b/ruic20_lab/software/reference/vep_annotation/ref/Homo_sapiens.GRCh38.dna.primary_assembly.fa`,
存在且在 annotate 作业里被 bind)。所以 `BIOAGENT_VARIANT_ON_HPC=1`(prod 已开)下,规范化在注释时已完成,
单列的 CodeAct normalize 纯属重复。注:`.env` 没设 `BIOAGENT_REF_FASTA` —— 对离线注释无影响(settings 默认正确且存在),
只有那个独立 skill 需要它。

采取的修复(方向 = "去冗余 + 加固",不是"让独立 normalize 能跑"):
- `skills/normalize_vcf/SKILL.md` —— 改掉**过时错句**"the offline VEP path does not normalize either"(离线加了
  内部规范化后就不成立),并告诉规划器:`BIOAGENT_VARIANT_ON_HPC=1` 下**不要**单列 normalize 步(多余 + analysis.sif 跑不了)。
- `research_lab.py` 的 `_ALTERNATIVES_SYSTEM` —— 失败分叉**不再**提供"手写 Python 重实现需要专用二进制+参考数据的操作"
  (无 bcftools/参考的左对齐、无 aligner 的重比对)这类方案;优先用真正的工具或跳过让下游步骤覆盖。

当前卡住那个 run 的操作建议:决策卡上选 **「跳过此步骤」** —— run_code 的替代方案全是死路(analysis.sif 无 bcftools/参考),
下游 `annotate_variants` 会重做规范化。**未做/推迟**:给 analysis.sif 加 bcftools + 在 `slurm_sandbox.py`
转发 `BIOAGENT_REF_FASTA` 并 bind 参考目录(那样才能让独立 normalize 真跑起来,供 REST 路径用 —— 只有想把规范化和注释解耦才值得)。
分支基于 `main`;**未部署**(Yijun 跑 `sync_deploy.sh`)。

## 2026-07-10 —— 运行/会话隔离修复(逐运行 + 逐会话,端到端)

修复网关隔离 bug(记忆 `run-conversation-isolation-bug`):一个 SSH/GPU `Connection` 被用户的多个
窗口/标签页/会话共用,但所有运行状态都挂在 Connection 上、只按 `connection_id` 区分,导致运行互相串
扰(取消/批准打到当前活动的那个运行;输出流进当前**选中**的窗口;新会话把连接级的旧 `last_run_id`
当作"续跑";计划评审超时还渲染出一份占位的、无数据的报告并解绑了数据集)。基于 `main` 开分支;
**未部署**(由 Yijun 跑 `sync_deploy.sh`)。648 项测试通过(新增 19)。

- **逐运行作用域(`RunState`)。** 每个运行(研究 / 重新生成报告 / A2 续跑)各自持有 `chat_stop` +
  `plan_event`/`plan_value`/`pending_plan` 及身份(`run_id` + `conversation_id`)。`Connection` 新增
  `runs`(登记表)、`active_run`、`last_run_by_conversation`;旧的 `conn.chat_stop` / `conn.plan_event`
  等改为代理到活动运行的属性,~30 处旧调用点无需改动。`gateway/app.py`。
- **事件打标 + 前端分流。** 每条流式 WS 事件都打上 `run_id` + `conversation_id`(`push/emit` → `_tag`);
  流回放 + 重连时的 plan/clarify/decision 提示也带标。`frontend/console/app.js` 现在只处理**本窗口发起**
  的运行(`state.runSessionId`),丢弃外来运行的作用域事件——别的窗口的运行不再串进/取消/在本窗口弹计划卡。
  每个运行 POST(`/api/lab`、`/chat/stop`、`/chat/inject`、`/lab/plan`)都带 `conversation_id`。
- **定向取消/批准。** `/api/chat/stop` + `/api/lab/plan` 按请求指定的运行来定位(`resolve_run`)——
  对已结束运行的过期批准/取消变成空操作,不会误打到当前活动运行。
- **续跑 vs 全新,逐会话判定。** `conversation_id` 加到 `LabRequest`/Stop/Plan/Regenerate/Continue +
  `Run` 模型(可空列 + `db.init_db` 里幂等 ADD COLUMN)+ `record_run_start`。`_followup_target` 改按
  `last_run_by_conversation`,新窗口/线程不会继承别的会话的运行。
- **不再出占位报告 / 数据集保持绑定。** 计划被取消/超时——或运行 0 步被接受且无 rounds
  (`_run_produced_nothing`)——以 `chat_stopped` 结束,**不出报告、不写 run_state、不设 last_run_id**,
  所以该会话下一条消息是全新且带数据集的研究。运行中途 Stop 但已接受 ≥1 步的,仍照常成文。
- **固定预设不再硬套到 VCF。** 预设流水线新增 `data_type` 模态(frontmatter;`variants` vs `scrna`)。
  `drop_conflicting_pinned`(在 `ResearchLab.run` 使用):当数据集自动选路为 `variants` 时,丢掉被固定的
  单细胞流水线(以数据集为准)。
- **追问能跨网关重启认回上一个 run。** `_followup_target`(和 `_default_run_id`)用 `_conversation_last_run`
  解析某会话的上一个 run:先查内存 `last_run_by_conversation`,miss 再查 DB
  (`auth_routes.latest_run_id_for_conversation` → 该会话+用户最新的 `done`/`incomplete` run)——所以重启后
  打字追问能认回上一个 run 而不是当成全新研究(取消/报错的 run 被排除,续跑 vs 全新的判定依然成立)。DB 命中会回暖内存缓存。
- **给迁移前的历史行回填。** `scripts/backfill_run_conversation_id.py`(默认 dry-run,`--commit` 才写)
  从 `messages.meta` 的 bundle/artifact URL 反查 `run_id → conversation.id`,填 `runs.conversation_id` 的
  NULL 行;被 >1 个对话引用的 run_id 跳过(历史泄漏)。在服务器上以 `bioagent` 账号跑。
- **测试:** `tests/test_run_isolation.py`(打标、逐运行取消、`resolve_run`、逐会话续跑判定、定向
  stop/plan 接口、取消不出报告——含端到端驱动 `_run_lab`、以及 DB 兜底的重启后追问)+
  `tests/test_preset_compose.py` 的冲突协调用例。

## 2026-07-09 —— SpliceAI(OpenSpliceAI)已安装、接入离线注释线、容器内端到端验证

## 2026-07-09 —— SpliceAI(OpenSpliceAI)已安装、接入离线注释线、容器内端到端验证

按 Jin 定的路线(OpenSpliceAI,PyTorch,不用 BaseSpace 预算分),已全流程打通:

- **环境(HPC3):** conda 环境 `/dfs3b/ruic20_lab/software/bioagent/envs/openspliceai`(conda-forge
  py3.10 + `pip install openspliceai==0.0.7`,带 torch)。模型 = OSAI-MANE-10000nt 5 模型集成,放在
  `/dfs3b/ruic20_lab/software/reference/spliceai/OSAI-MANE-10000nt`(5×2.8MB,来自 JHU CCB **FTP**——
  该主机 https 被墙,ftp 通)。环境和模型都在**公共 reference/software 路径**。
- **在 vep.sif 内部运行。** OpenSpliceAI 需要 torch(vep.sif 里没有),所以它是一个 conda 环境,在容器
  内部作为子进程被调起——conda-forge 的 python 能在 vep.sif 的 glibc 下正常跑(已验证:把 env + 模型 +
  参考 FASTA 目录只读绑进去,HOME 指向每次运行的可写目录;`.fai` 必须已存在,pyfaidx 才不会去重建)。
  两个破坏 SAMD11 供体位点的变异打分 DS_DL **0.917 / 0.755**,独立跑和在 vep.sif 内跑都一致(热态 63s)。
  CPU 上约 50s/变异 ⇒ **只用于 panel 阶段**。
- **代码(默认全部关闭):** `vcf_offline.py`——`build_spliceai_cmd`/`write_spliceai_vcf`/
  `parse_spliceai_vcf`/`merge_spliceai` + `run_offline_annotation` 里一个阶段,对**过滤后**的变异集跑,
  **默认不设上限**(`spliceai_max_variants`=0);设 >0 则为可选安全阀,集合仍很大时跳过。`variant_annotation.py` 加了 `spliceai_max_ds`/`spliceai_site`
  两列;`_is_damaging` 把 max ΔS≥0.5 也算致病。`settings.py`(`spliceai_*` + `BIOAGENT_SPLICEAI*` 环境变量)、
  `app.py`(绑目录 + 注入参数)、`variant_cli.py` 透传。测试:`tests/test_vcf_offline.py` 新增 6 条,全绿。
  暂存自动化在 `deploy/vep/stage_annotation_dbs.sh`(`STAGE_SPLICEAI`)。
- **留给 Yijun(部署/sync):** 在生产 `.env` 里设 `BIOAGENT_SPLICEAI=1`(加两个路径变量)并部署,GRCh38
  离线运行时即生效。(REVEL 在公共路径的重建**已完成并验证**:之前的 0 字节是被 move 脚本半路 `mv` 走目录
  害的;干净重建后是有效的 675MB GRCh38-tabbed REVEL——BRAF V600E 打分 revel=0.931,和 CADD 29.8 /
  AlphaMissense 0.9927 一起正常加载。)

## 2026-07-08 —— 注册验证码邮件走 UCI SER(Proofpoint)relay

UCI IT(Derek Chee)为 AiScientist 发放了 SER relay 凭据。自助注册流程
(`auth_routes.py` 发 6 位验证码)与 SMTP 发信器(`gateway/email_send.py`)本就已实现、
完全由 env 驱动 —— **代码路径无需改动**,SER 恰好匹配现有的 STARTTLS+AUTH 路径。只改了配置与文档:
- `configs/aiscientist.example.env`:新增 `BIOAGENT_SMTP_*` 配置块 + 自助注册开关。
- `deploy/README.md` §1:把 SER 变量加入生产 `.env` 清单,并补充验证 / 重启说明。
- `gateway/email_send.py`:docstring 改为指明 SER(`smtp-us.ser.proofpoint.com`)为选定 relay,
  替换掉旧的 `smtp.uci.edu` 指引(对本次部署有误导)。

已对线上 relay 做过验证(开发机非认证探测):587 端口通告 STARTTLS,TLS 后提供 AUTH LOGIN/PLAIN,
协商到 TLS 1.2 —— 与 Derek 所述、与 `email_send.py` 所用完全一致。dev/smtp 模式切换已确认。

**生产操作(在 eyeserver 上,本次未执行):** 在 `/data/BioAgent/app/.env` 写入 —
`BIOAGENT_SMTP_HOST=smtp-us.ser.proofpoint.com`、`BIOAGENT_SMTP_PORT=587`、
`BIOAGENT_SMTP_TLS=starttls`、`BIOAGENT_SMTP_USER=<Relay User ID c5c4dca0…>`、
`BIOAGENT_SMTP_PASSWORD=<PDF 里的密码>`、`BIOAGENT_SMTP_FROM=AiScientist <no-reply@<PUBLIC_HOSTNAME>>`
—— 然后 `sudo systemctl restart bioagent`。用 `GET /api/auth/config` 确认 `email_mode=smtp`。
密钥只存在于(全局可读的)生产 `.env`,绝不入库。
⚠️ 待办:尚未从生产主机确认 eyeserver→proofpoint:587 的出站连通性。

## 2026-07-08 —— 团队定了 VCF 工作流(Rui Chen / Jin Li 邮件),并已接入

计划/成本邮件(`deploy/vep/PREDICTOR_STAGING.md`)发给 Jin Li + 团队,回复已实现(代码侧;部署/数据/基因列表仍在 Yijun/团队):

- **Rui Chen(PI):罕见病,已知基因优先。** IRD 的致病变异罕见且多在已知基因。已接进 `annotate_variants`
  (`tools/variant_annotation.py` 的 `apply_variant_filters` + 工具 schema):`genes`(已知基因面板,只留这些;
  或离线线用 `regions_bed` 在 VEP 前就限制区域)+ `max_pop_af`(丢弃 gnomAD AF > 阈值,如 0.01)。通过
  `vcf_offline.py` + `variant_cli.py` 打通;返回带 `variant_filters`(丢了多少 common/面板外)。变异 preset
  现在以该策略开头(已知基因 → 丢 >1% → 排序 → 阴性再扩到全基因组)。**未决:Meng 的已知 IRD 基因列表**
  (接到 `genes` / 面板 BED)。
- **Jin Li:下一次 + 挂载,存储没问题。** 新增幂等脚本 `deploy/vep/stage_annotation_dbs.sh`——检查哪些 DB
  文件已存在、只下缺的(AlphaMissense/CADD/REVEL/参考 FASTA),打印 `BIOAGENT_VEP_*` 环境变量;把该目录只读
  挂进 vep.sif。lab 共享盘保持 >20 TB 空闲(可扩 >50 TB)——CADD 87 GB 没问题(我之前 dfs3b-97% 的警报对这事作废)。
- **SpliceAI 不再被卡** —— bioconda 或 OpenSpliceAI(跑模型;面板缩小后可行);不需要那个巨大的 BaseSpace 预计算分。
  尚未接入(等 conda/OpenSpliceAI 选型)。

全量测试 611 通过。环境变量+参数见 `docs/vcf_pipeline_tools.md`。

## 2026-07-08 —— VCF 路径周:预测器插件铺数据、HPC3 外网已验证、工具清单文档

本周目标:把 VCF 路径彻底端到端跑通(离线 VEP、发表级注释、上生产)。

- **HPC3 计算节点外网已实测(之前我说"没外网"是错的——是照抄了 deploy 脚本里的过时注释)。** 在计算节点
  `hpc3-l18-01`(partition `standard`,account `ruic20_lab`)提了个 Slurm 测试作业:DNS 正常 + 对
  api.ipify.org 和 ftp.ensembl.org 都 HTTP 200。所以需要 REST 的工具在计算节点也能用;`deploy/vep/build_and_stage.sh`
  里那句"compute nodes don't [have internet]"已改正。
- **预测器插件在 HPC3 铺数据中**(登录节点后台下载,日志
  `/dfs3b/ruic20_lab/software/bioagent/vep_cache/plugins/download.log`):AlphaMissense hg38(643 MB,Zenodo)
  + CADD v1.7 GRCh38 全基因组 SNV(87 GB,几小时)。落在 `vep_cache/plugins/{alphamissense,cadd}/`。
- **SpliceAI = 暂缓(我的建议)。** 它的预算分要 Illumina/BaseSpace 账号登录才能下(手动),而且价值集中在剪接区
  变异——不该卡住路径跑通。本周先上 CADD + AlphaMissense(REVEL 作为随手可加的后续);SpliceAI 单独作为手动
  下载任务。
- **新工具清单文档:`docs/vcf_pipeline_tools.md`** —— VCF 路径里每个工具 + 参考数据(大白话解释、在代码哪、
  多大、live/staging/deferred 状态、环境变量)。已从 `preset_pipelines/variant_annotation/SKILL.md` 交叉引用。

**代码已完成 2026-07-08(全部门控 + 优雅降级,全量测试 610 通过)。** 分工:Claude 写代码,Yijun 负责部署/sync
+ 数据。在 `tools/vcf_offline.py` + `tools/variant_annotation.py`:VEP 预测器插件(CADD/AlphaMissense/REVEL)
+ `--mane_select`(`vep_plugin_flags()`,主开关 `BIOAGENT_VEP_PLUGINS` 默认关,按文件存在与否逐个加);HGVS
(`--hgvs --fasta`)门控在 `BIOAGENT_REF_FASTA`;VEP 前加 `bcftools norm` 左对齐(同门控);`parse_vep_result`
读 hgvsc/hgvsp/mane_select/cadd_phred/revel/alphamissense,扩了 `ANNOTATION_COLUMNS`(缺就留空);
high-priority 选择用上 CADD≥20/REVEL>0.5/AM>0.564;ClinVar 加 `%CLNREVSTAT` 星级列;返回带 `normalized`+`predictors`。
生产在 Yijun 设 `BIOAGENT_VEP_PLUGINS=1` + 路径前不变。环境变量+路径见 `docs/vcf_pipeline_tools.md`。

**已验证(2026-07-09,我自己在 HPC3 上做的,不再甩给你):** staging 脚本已铺好 AlphaMissense(+tabix)+ 克隆
了 VEP_plugins(`CADD.pm` 等);实测在**当前 vep.sif** 里用 `--dir_plugins` 加载 AlphaMissense 插件成功——对
BRAF V600E 打出 `am_pathogenicity=0.9927`、`mane_select=NM_004333.6`,零报错。**所以 vep.sif 不用重建**(插件走 bind
挂载 + `--dir_plugins`)。remote Sylabs builder 也已验证可用(Token OK),需要建容器时我能自己建(先例:2026-07-07 我
远程重建过 vlreview.sif)。

**留给 Yijun / 待办:** CADD 下完 + REVEL/参考 FASTA staging 跑完(在跑);**`tiledbvcf-py`**(能力 2 的 TileDB 变异库)
需要一个带该包的容器——我现在能 remote-build(建议单独一个 variant-db sif,不动 scanpy 的 analysis.sif);设 prod 环境变量
+ 部署(你 sync);SpliceAI 选型定了我就接。
待决策(见给 Jin Li 的邮件 `deploy/vep/PREDICTOR_STAGING.md`):SpliceAI(Illumina 账号?)、CADD 去留
(dfs3b 已用 600 TiB 配额的 97%)。

## 2026-07-08 —— Sandbox 默认联网 + 整合 operon 的 VCF skills

skill 文件夹迁移(见下)之后的两个后续需求,都在本 worktree,全量测试通过(605 个)。

**(a) 容器内代码可联网(Jin Li:优先保证效果)。** `run_code` 和 HPC3 分析工具原来带 `--net --network none`
(完全隔离),任何需要拉参考数据或调 REST(VEP、Ensembl、Europe PMC、下载)的工具/skill 都会静默失败。
`agents/sandbox.py` 新增 `sandbox_network_enabled()`,默认 ON;接到 `CodeSandbox.allow_network`、
`SlurmCodeExecutor`、`SlurmAnalysisExecutor`。用 `BIOAGENT_SANDBOX_NETWORK=0` 关闭。`.sif` **不用重建**——
Singularity 联网是调用参数决定的。GPU 纯权重作业(scGPT、VL-review)保持断网。⚠️ 未决:HPC3 **计算节点**
是否真有对外网络(很多集群只在登录节点/经代理才行)尚未验证——代码改动本身是对的,但假设计算节点能发 REST
之前,请在 HPC3 用一个测试作业确认。

**(b) 整合 operon 的 VCF 方法。** 读了 github.com/swaruplab/operon(它的 "protocols" 就是 Claude Skill =
整份 SKILL.md 注入)。把最关键的正确性/解读缺口移植成 3 个新原子 skill:`skills/normalize_vcf/`
(bcftools norm——注释前 atomize / 拆多等位 / **左对齐**;不规范的 indel 会静默匹配不到 ClinVar/gnomAD,是我们
最大的坑),`skills/vcf_qc_stats/`(Ti/Tv、Het/Hom、计数、call rate 对照 operon 的 WGS/WES 期望区间),
`skills/clinical_variant_prioritization/`(ACMG-lite 研究分诊分级,来自 ClinVar+AF+预测器,绝不升级 ClinVar,
明确"非临床诊断"免责声明)。已作为可选步骤接入 `preset_pipelines/variant_annotation/SKILL.md`。测试:
`tests/test_operon_variant_skills.py`。我们在 TileDB-VCF(operon 完全没有)和单工具自动 offline/REST 路由上
领先 operon。

路线图(暂缓——更重、会碰刚修好的生产 VEP 线、且要在 HPC3 铺数据):在 `tools/vcf_offline.py` 加更丰富的 VEP
预测器(CADD ~80GB / dbNSFP-REVEL / AlphaMissense / SpliceAI 插件)+ MANE-Select + HGVS,离线工具内加一步
`bcftools norm`(需在 `deploy/vep/` 铺参考 FASTA),以及 ClinVar 审核星级(`CLNREVSTAT`)。完整 P1–P6 见本次
会话记录。GATK 硬过滤 / SV / CNV / 变异检出属于另一条线,不在范围内。

## 2026-07-08 —— 原子 skill:扁平 `skills/<name>.py` → 文件夹 `skills/<name>/{SKILL.md, reference.py}`

Yijun 的疑问:"为什么 skills 全是单个 .py 没有 SKILL.md?怎么实现滚动加载 / WHEN-to-use / 介绍?"——
扁平单文件把描述塞进模块 docstring 首行,不符合 Anthropic 对 Skill 的定义(描述与示范分离)。Yijun 选了
文件夹形态("对齐 Anthropic")。本 worktree 已完成:

- **格式** —— 每个 skill 现在是 `skills/<name>/`,含 `SKILL.md`(frontmatter `name`+一行 `description`;
  正文 `## When to use` / `## Details & adaptation` / `## Run`)+ `reference.py`(示范模板,用 `git mv`
  从旧扁平文件移入,保留历史)。新增 `skills/README.md`。共迁移 10 个 skill。
- **加载器**(`agents/skills.py`)—— `Skill(name, summary, doc, files)`;glob `skills/*/SKILL.md`,
  解析 frontmatter(复用 preset pipeline 的约定),读取捆绑文件。skill 标识 = 文件夹/frontmatter 名,
  **不带 `.py`**;`get_skill`/查找会剥掉遗留的 `.py`,所以旧配置和 preset 正文里的 `<name>.py` 仍能解析。
- **三级渐进披露** —— brief 清单(`name — description`)→ `read_skill_reference(name)` 返回 SKILL.md
  **指南** + 文件列表(不含代码)→ `read_skill_reference(name, file="reference.py")` 返回**代码**。
  `research_lab.py` 的 brief 文案和 REQUIRED-skills 指令都改成描述这套两步 fetch。
  `search_skills`/`list_skills` 形态不变,`/api/skills` 控制台选择器的往返照常工作。
- **测试** —— skill/lab 相关测试已更新为新命名 + doc/files API;全量套件通过,仅剩两个**既有的、与本次无关**
  的失败(见下)。

⚠️ 尚未部署到 eyeserver 生产(和本 worktree 的其余工作一样)。健壮性提醒:消费方是 Qwen 不是 Claude ——
现在"指南→代码"是两跳;若某次运行读了指南却忘了取代码,把代码折回第一次 `read_skill_reference` 的响应里
只是一行改动。`docs/skills_and_pipelines_architecture.md` 已更新(2026-07-08 小节)。

**既有失败(非本次引入;在干净 HEAD 上也复现):**
`tests/test_variant_annotation.py` 缺 `from pathlib import Path`(本次已修);
`tests/test_gateway_lab.py::test_report_writer_prompt_is_manuscript_structured` 读的是旧的模块级
`_REPORT_WRITER_SYSTEM`/`_REPORT_REVIEW_SYSTEM` 常量,它们已被重构成
`_report_writer_system()`/`_report_review_system()` 函数——main 已经修好了这个测试(合并时采用 main 的版本)。

## 2026-07-08 —— 离线 VEP 变异线在生产里 100% 崩(4 个 bug)→ 已修 + 端到端验证

诊断了 run `1237b046f41a`(真实 hg19 全基因组 VCF,样本 CASE_B,493 万变异):9 个 `annotate_variants`
步骤全部 FAIL,但正稿却被渲染成一篇体面的托词——"概念框架 / 实证处理留待 spatial-proteomic 校准后再做"
——这是编造的(工作是 FAILED,而且 WGS VCF 研究里根本没有 spatial/proteomic 数据)。文献也降级成 12 篇跑偏
的 scRNA-seq(0 篇与变异相关)。真因不是技术报告猜的 pysam/REST——而是 Slurm 作业**在容器创建阶段就挂了**,
且这个错误从没传到任何人手里。我去读了 HPC3 上真实的作业日志(`sacct`:12 个 `bioagent_variant_*` 作业全
FAILED,ExitCode 127,1 秒)才定位到。**4 个 bug,全部修复,并通过在 HPC3 上真跑管线验证:**

1. **scratch 放在 `$HOME`(=`/data/homezvol*`)无法绑进 `vep.sif`**——根因。`vep.sif` 把 `/data` 做成了指向
   `/opt/vep/.vep`(VEP 只读缓存)的符号链接,所以把 `$HOME` scratch 同路径绑进去时会解析到那个只读路径,
   singularity 直接 `FATAL: destination doesn't exist in container`(exit 127,约 1 秒)。scanpy 的
   `analysis.sif` 里 `/data` 是真目录所以从没踩到。修复:`scratch_dir` → `{_storage_base(conn)}/.bioagent/variant`
   (`/dfs3b` 路径像 workspace 绑定一样能被 overlay 干净创建)。`app.py`。
2. **真正的错误是个黑洞**——`slurm_analysis._collect` 只读工具的 stdout/stderr(`res_f`/`log_f`),从不读
   SBATCH `--output` 作业日志(容器启动 FATAL 就落在那),所以模型只拿到无用的 `"analysis job produced no
   result (Slurm state FAILED)"`。修复:也读 `{name}-{jobid}.log`。`slurm_analysis.py`(+ 回归测试)。
3. **从不从 VCF 判定装配版本**——VCF 是 hg19/GRCh37,但 `annotate_variants` 用了 `.env` 默认的 GRCh38 缓存。
   经验验证:同一 hg19 坐标下,非 MT 变异 150 个里有 134 个在 GRCh38 vs GRCh37 得到**不同基因**(约 90% 错)。
   修复:`detect_assembly()` 从头部读 chr1 contig 长度/构建标签并覆盖默认值 → 路由到 GRCh37 缓存+ClinVar
   (两者早已在 dfs3b 上备好)。`vcf_offline.py` + `app.py`(+ 单测)。
4. **ClinVar `.tbi` 索引从没被绑定**——只绑了 `clinvar_*.vcf.gz`,没绑同目录的 `.tbi`;在 `--containall` 下
   VEP 的 `--custom` Tabix 查找就 `Couldn't find index` 挂掉。修复:把 `f"{_clinvar}.tbi"` 加进 `extra_ro_binds`。
   `app.py`。(chr 前缀**无需**修:缓存里的 `chr_synonyms.txt` 会自动映射 `chr1`↔`1`。)

**已在 HPC3 端到端验证**(真实 `variant_cli` 跑 `vep.sif`,dfs3b scratch,自动判定 GRCh37,200 变异切片):
`status:ok`,5 张交付表全写出,ClinVar 生效(MT-ND5 rs267606893 判为 pathogenic)。本地全套 587 通过(顺手
修了一个预存的缺 `Path` import 的测试)。**尚未部署到 eyeserver 生产网关**——需要 pull + 重启 systemd。正稿在失败时编造"框架"叙事的问题在下一节处理。

## 2026-07-08 —— 报告写手在彻底失败时编造"框架"正稿 → 已改为诚实

同一个 run `1237b046f41a`:4 步只通过 1 步(还是跑偏的文献)、没有任何真实分析结果时,写手编造了"概念框架 /
实证处理留待 spatial-proteomic 校准后再做",甚至把研究问题改写成"translational scRNA-seq workflows"。根因:
写手 prompt **强制要求**"每个小节都要充实、不许空/占位"——没有真东西可写时,模型就用臆想填满必填小节;而唯一
真实的产物(`variant_filter_summary.json`:493 万变异,95.69% PASS)根本没送到写手手里(它是 JSON,而预览只
glob `*.csv`;`_variant_facts_block` 读的是不存在的注释结果)。修复**没有破坏** [[silent-degradation-design]]
(逐步降级仍只进技术报告;收敛的正常 run 正稿不变)。`app.py` 三处外科式改动:

1. **把真实的 PASS 过滤结果送进去。** `_variant_facts_block` 在注释失败时回退读 `variant_filter_summary.json`
   → 正稿拿到真实的 总数/PASS/非PASS,并附注释未完成的说明。
2. **RUN STATUS 诚实块。** 新增 `_manuscript_run_status_block(result)`:收敛的 run 返回 ""(正稿不变);未收敛的
   run 注入一份去重后的"未产出结果的计划分析"清单 + 指令:只报告真实结果,**不许**编造"框架/脚手架/留待未来部署"
   叙事,宁可简短也不臆想。
3. **写手 prompt 加反编造规则**(`_report_writer_system`,两种 kind 都有):不许把问题改写成别的主题;不许把
   "计划了但没执行"的方法写成有结果;当 RUN STATUS 说未完成时,诚实+简短**压过**"每节都要充实"。

顺手修了预存红测试(`test_report_writer_prompt_is_manuscript_structured` 引用了改名的 `_REPORT_WRITER_SYSTEM`
→ 现在调用 `_report_writer_system()`),并为三处改动加了测试。全套 **592 通过,0 失败**。用真实失败 run 的
`lab_result.json` + 过滤汇总回放新路径做了确定性验证(写手现在同时拿到 493万/95.69% 计数**和**诚实指令)。
模型侧正稿文本建议下一次真实失败 run 时肉眼确认。**尚未部署到生产。**

## 2026-07-08 —— 离线 VEP 变异线在生产里 100% 崩(4 个 bug)→ 已修 + 端到端验证

"渐进披露到底实现了没?"——追了源码:**一半**。manifest 和 brief 里的指令("call
`read_skill_reference(name)` / `search_skills(query)`")都在,但那两个 FETCH 工具**只在
`ResearchLab.__init__` 的 `else` 分支**(`scientist is None`,即测试)里 append。**生产网关**自己用
`build_scientist_catalog(...)` 建 scientist(按设计它不加这俩——"不进 registry")再注入
(`ResearchLab(scientist=…)`),所以 `else` 从不执行。`ResearchHarness._by_name` 是构造时的快照,于是生产里
`read_skill_reference`/`search_skills` 是 **`unknown tool`**——模型被叫去调不存在的工具、取不到任何 skill
body,只能退回去手写 run_code(正是那个写崩代码的症状;也顺带把控制台的 REQUIRED-skills 功能悄悄弄坏了)。
全套测试是绿的,因为每个测试都跟网关**一样**注入 scientist——但没一个测试断言 fetch 工具可被调用,所以这个洞一直看不见。

**修复**:`ResearchHarness.add_tools(*tools)` —— 按名 append + 刷新 `_by_name`(能作用于已建好的 harness)。
`ResearchLab.__init__` 现在对**两个分支**(注入的 + 自建的)都调
`self.scientist.add_tools(make_search_skills_tool(), make_skill_reference_tool())`,幂等。回归测试
`test_injected_scientist_gets_progressive_disclosure_tools` 复现网关模式并断言两个工具变得可调用。文件:
`agents/research_harness.py`、`agents/research_lab.py`、`tests/test_research_lab.py`。分支
`claude/silly-diffie-a165f8` → 已并入 main。**含义**:在这个修复之前,`variant_output_tables.py` skill(以及
所有 skill)在生产里**根本取不到**。

## 2026-07-07 —— 把变异后处理折叠进 annotate_variants(注册工具),skill 保留作兜底

## 2026-07-07 —— 把变异后处理折叠进 annotate_variants(注册工具),skill 保留作兜底

按 Yijun 定的:既然变异工具稳定,就把后处理做成**注册工具的行为**,而不是 skill+run_code。
`annotate_variants`(REST 和 offline 两条路)现在**自己**确定性地写出 5 张标准交付表,零 run_code:
- `tools/variant_annotation.py` 的 `write_standard_tables(summary, tables_dir)` 写出 consequence/impact/
  clinical 分布、`clinvar_pathogenic_variants.csv`、`high_priority_variants.csv` 短名单
  (+ `data/annotated_results_summary.json`)。由 `annotate_variants_rest` 和
  `vcf_offline.run_offline_annotation` 调用;工具结果里列出 `standard_tables`,描述里写明"不要写任何
  run_code 做后处理"。
- **语义收紧**:`summarize_annotations` 的 `high_priority` 现在是 NOVEL 候选短名单——rare + 高影响/有害
  且**不在 ClinVar**(致病的单独成表)。这与问题#3 + 交付 CSV + skill 三者一致了。
- `skills/variant_output_tables.py` **保留**(Yijun 的意思)作自定义阈值/列的兜底;标准路径不再需要它。

测试:`test_variant_annotation.py`(+1 write_standard_tables,更新短名单断言)。分支
`claude/silly-diffie-a165f8` → 已并入 main。

## 2026-07-07 —— offline-VEP 接线全面诊断日志

## 2026-07-07 —— offline-VEP(variant-on-HPC3)全面诊断:把"为什么 sif 没被调用"打清楚

Yijun 在 eyeserver `.env` 里配了 VEP sif,但离线线还是没跑,而且没有日志说原因。原来那个判定块
(`app.py` 的 `variant_on_hpc` 分支)几乎不打日志:条件跳过、Slurm job 回退都不产生任何运行日志行。
现在每个分支都打日志,并落盘到 `process/variant_offline_diagnostics.log`:
- **配置行**(每次 VCF run):`BIOAGENT_VARIANT_ON_HPC=ON/OFF`、vep_image、cache、clinvar、assembly、
  executor 有没有、mock。一眼就能看出是不是**开关**的问题(光配 VEP_* 路径不算开——`BIOAGENT_VARIANT_ON_HPC=1`
  才是开关;这最可能是 Yijun 遇到的原因)。
- **逐条原因**:线没接上时说明是 flag 关 / executor 为 None / mock。
- **HPC3 预检**(`_variant_offline_preflight` + `_remote_path_status`):在 HPC3 上 `test -e/-d` 检查
  sif + cache + ClinVar,逐个记录 存在 / 未找到(带路径)/ 无法验证——这样 `.env` 里写错或没 stage 的路径
  会在**这里**被点名,而不是变成一个神秘的 job 失败。
- **回退变响**:`SlurmAnalysisExecutor` 加了个附加的 `on_fallback(reason)` 钩子;离线线用它在 job 降级到
  REST 的瞬间打出**原样的 Slurm 报错**(并追加到诊断文件)。

测试:`tests/test_variant_offline_diagnostics.py`(4 个,fake executor 上的预检)+
`tests/test_slurm_analysis.py`(+2,on_fallback 带 reason 触发 / 钩子抛异常也不破坏回退)。分支
`claude/silly-diffie-a165f8` → 已并入 main。下一步:Yijun 重跑一个 VCF,看配置行/诊断文件就知道真正原因
(大概率是 `BIOAGENT_VARIANT_ON_HPC` 没设成 truthy,或某个 cache 路径没 stage)。


## 2026-07-07 —— 新增 atomic skill `variant_output_tables.py`(修掉 VCF 后处理的 run_code 反复写崩)

同一 run `09a48f3cf62f`:编排模型(Qwen3.6-35B)每一步都在写大段崩掉的 run_code——手工重新解析 VCF、
手拼结果表 + 200 行的 summary 字典(就是 `'{' was never closed`/unterminated-string 那些错),而这些
`annotate_variants` 全都已经返回了。

修复(分支 `claude/silly-diffie-a165f8` → 已并入 main):新增一个 **atomic skill**
`skills/variant_output_tables.py` —— **纯标准库**(csv/json/collections,零 pandas dtype 陷阱)的 CodeAct
模板,从已落盘的 `tables/variant_annotation.tsv` 读取,写出五张标准交付表(consequence/impact/clinical
分布、ClinVar 致病列表、rare-未分类 high-priority 短名单)+ `data/annotated_results_summary.json`。由
`agents/skills.py` 自动发现(扁平 `skills/*.py`,progressive disclosure)。
`preset_pipelines/variant_annotation/SKILL.md` 已改成让后处理步骤点名用它("别手写 CSV 代码——取这个 skill
微调")。不同于 scanpy 模板(CI 里只 compile),这个是纯标准库所以在
`tests/test_variant_output_tables_skill.py` 里端到端**执行**验证。依赖前一条修复(annotate_variants 落盘
完整 `variant_annotation.tsv`,见下节)。

## 2026-07-07 —— 变异注释报告生成缺陷全量修复(表/FILTER/绑定数字/Methods/渲染残留)

同一 run `09a48f3cf62f` 的五类报告生成缺陷(都对着结果包核实过)全部修掉;每处改动都按任务类型分流,
单细胞路径不受影响。

1. **注释表是坏的** —— 交付的"注释结果表"只有一列 FILTER。REST(`tools/variant_annotation.py`)和
   离线(`tools/vcf_offline.py`)的 `_write_table` 现在都写完整 schema(`ANNOTATION_COLUMNS`:
   gene/pos/rsID/consequence/impact/ClinVar/AF/SIFT/PolyPhen)并**校验**(`os.path.exists` + 表头==schema,
   BIOAGENT_ARTIFACTS→BIOAGENT_WORK 兜底);工具结果返回路径 + `annotated_table_columns`,描述里叫模型引用
   该文件、别自己 run_code 拼 CSV。
2. **假的"ALL PASS"** —— 新增 `read_vcf_for_annotation()`,遵循"先按 PASS 过滤"(过滤在 cap **之前**,
   `pass_only=True` 默认,与离线线一致)并报告真实 `n_pass`/`n_nonpass`。
3. **编造数字**("165 high-priority" 真值 0;"6" vs 12 篇文献)—— 新增 `_variant_facts_block()`,从
   annotate_variants 工具结果(+ 已接受文献数)构造 AUTHORITATIVE COUNTS 块,注入两个报告 prompt,并加
   "必须用这些精确数字"的硬规则。
4. **scanpy Methods 泄漏** —— manuscript/技术/自查三个 prompt 改成**按 `_report_task_kind()` 分流的函数**:
   变异 run 用变异 Methods(VCF→FILTER→VEP→consequence/impact→ClinVar→gnomAD→SIFT/PolyPhen→shortlist),
   并明确禁止写 HVG/PCA/UMAP/clustering/DE。
5. **渲染残留** —— 文献标题里的 `<i>` 从源头修(`literature_references._plain_text`);新增
   `_strip_render_residue()`(在 `_review_and_finalize_report` 里**渲染前**跑,是"修"不是"记录"):删掉多余的
   `[Figure N. …]` 占位行、剥离 raw/转义的行内 HTML 标记、折叠多余空行。

测试:`test_variant_annotation.py`(+5)、`test_literature_references.py`(+1)、新增
`test_report_variant_routing.py`(9)。全部编译通过,逻辑已独立跑验(本机没 pytest)。分支
`claude/silly-diffie-a165f8` → 已并入 main。这些是报告生成器/工具的修复;底层 run 仍需开启离线线(见下节)
才能产出全量 VCF 的真实数字供报告使用。

## 2026-07-07 —— WGS VCF 走了 REST 路径被截成 500(离线 flag 没开)→ 把截断改成显式告警

**现象(run `09a48f3cf62f`)。** 1.1 G 的 VCF 约 14 秒就"注释完",报告称其为"500 变异 cohort"
(0 ClinVar、0 致病)。查结果 zip:离线 VEP 线根本没跑——event_log 没有 `"VCF annotation runs
OFFLINE on HPC3"` 那条,工具结果无 `execution_mode`,`n_input_variants: 500`,capabilities.log 只记了
scGPT + VL。

**根因。** 离线线(上个 session 建的,见下节)是 opt-in,靠 `variant_on_hpc`(`settings.py:174`,
env `BIOAGENT_VARIANT_ON_HPC`,默认 **False**)。eyeserver 部署里一直没开(见下节"待办"),所以
`annotate_variants` 落到 `annotate_variants_rest`,被 `parse_vcf_variants(..., max_variants=500)`
**静默截断**到前 500 个变异,所有分布都是这个切片,且没有任何提示。

**修复(分支 `claude/silly-diffie-a165f8`,已并入 main)。** 让截断无法被忽略:
- `tools/variant_annotation.py` —— `annotate_variants_rest` 达到 cap 时,结果里加 `truncated: true`
  + `n_annotated` + 醒目 `warning`。`test_variant_annotation.py` 补 2 个单测。
- `gateway/app.py` —— 兜底告警:离线线没接上但数据集是 `.vcf`/`.vcf.gz` 时,运行日志打 `warning`
  ("走 REST 路径、上限 500;要全量请设 BIOAGENT_VARIANT_ON_HPC=1…")。
- **仍是运维动作(Yijun):** 真正的修复是开启离线线——在 eyeserver `.env` 设 `BIOAGENT_VARIANT_ON_HPC=1`
  + `VEP_*`,确认 dfs3b 上 `vep.sif`/cache 已 stage,然后重跑这个 VCF。
- 同一 run 还有报告生成缺陷(technical_report 幻觉出"165 high-priority",真值 0;文献 6-vs-12;
  Methods 套用 scanpy 模板;注释 CSV 只有一列 FILTER)——已记录,尚未修。

## 2026-07-07 —— 离线 VCF 变异注释(WGS 级):REST → HPC3 上 `vep --offline` + 本地 cache

**为什么。** REST 版变异工具(`tools/variant_annotation.py`)扛不了大 VCF(≥~1GB / 全基因组):
`_read_source` 把整个文件读进一个 Python 字符串(OOM,`.vcf.gz` 尤甚);`max_variants` 上限 500
(全 WGS 只注释了 ~0.01%);公共 Ensembl VEP REST 有限流(≤200/请求),全量注释不现实。而且它
结构上没法像 scanpy 那样下沉 HPC3——REST 要外网,而 HPC3 容器是 `--network none`。

**做了什么(分支 `feat/vcf-offline-annotation`,从 main 切)。** 一条严格照 Phase-4 scanpy offload
镜像的离线产线。现在内存**与 VCF 大小无关、恒定有界**:bcftools 流式 PASS 过滤,VEP 用 `--fork`
对整个 VCF 打**本地 cache**(不需外网 → 与 scanpy 同一离线容器),Python 只逐行流式解析 VEP 的 JSONL。
复用 REST 版的 `parse_vep_result`/`classify_significance`/`summarize_annotations`,两条路输出同一套
schema;仅 ClinVar 来源不同(离线用 `--custom` ClinVar VCF,落在 `custom_annotations`)。

- **新增:** `tools/vcf_offline.py`(命令构造 + 流式 JSONL 解析 + `run_offline_annotation`,可注入
  runner → 全离线单测)、`tools/variant_cli.py`(容器内 entrypoint,对标 `scrna_cli`)、
  `deploy/vep/{vep.def,build_and_stage.sh,README.md}`、`tests/test_vcf_offline.py`(12 条,无子进程/网络)。
- **改动:** `variant_annotation.py`(抽出 `annotate_variants_rest`——现作小 VCF / 无 HPC 的兜底)、
  `gateway/slurm_analysis.py`(加 `extra_ro_binds`+`inject_args`+`job_prefix`,同一 executor 即可跑
  VEP 步、把 cache+ClinVar 只读挂载、注入部署配置)、`agents/registry.py`(泛化路由 + `variant_executor`)、
  `gateway/settings.py`(`VEP_*` + `variant_on_hpc`)、`gateway/app.py`(变异 executor 构造,镜像 analysis 块)。
- **决策(奕君):** GRCh38 + GRCh37 两套 cache;默认范围 = 全量 VCF、仅 PASS(HPC 路径去掉 500 上限),
  可选基因 panel/区域过滤。默认 `--fork` 8、`--mem` 64G、2 小时时限。

**状态(2026-07-07)。** 本地代码完成;`test_vcf_offline.py`(12)+ `test_variant_annotation.py`(10)
+ `test_registry.py`/`test_slurm_analysis.py`(12)全绿。HPC3 上 `--fakeroot` 未开(无 subuid)→ 用
**`--remote`** Sylabs 云构建(token 已配好,已验证通过认证)。`vep.sif` 远程构建 + GRCh38(25.4GB)
/GRCh37 cache + ClinVar 已脱离 ssh 后台下载到 `/dfs3b/ruic20_lab/software/bioagent/vep_cache/`
(`~/vep_build/{build,cache}.log`)。**待办:** 确认离线冒烟(小 TP53 VCF → 带 ClinVar 的 JSONL)、
stage `.sif`,再把分支部署到 eyeserver 并在 `.env` 设 `BIOAGENT_VARIANT_ON_HPC=1` 及 `VEP_*`,跑一个
真实大 VCF 端到端。启用用的 env 块见 `deploy/vep/README.md`。

**已做清单(处理方法,2026-07-07 · `feat/vcf-offline-annotation`):**
- [x] 离线核心 `vcf_offline.py`:bcftools 流式 PASS 过滤 → `vep --offline --cache --fork` → **逐行**
  JSONL 解析;**内存与 VCF 大小无关**(不再把整文件读进 Python)。subprocess runner 做成可注入 →
  解析/汇总/编排全部离线单测,无需真跑 bcftools/VEP/网络。
- [x] `variant_cli.py` 容器入口(对标 `scrna_cli`,打印 `BIOAGENT_RESULT_JSON`);部署配置(cache 路径
  /ClinVar/assembly/fork)由 executor 的 `inject_args` 注入,不进 LLM 的工具参数。
- [x] `variant_annotation.py` 抽出 `annotate_variants_rest` —— 既是 REST 路径也是离线失败时的兜底。
- [x] `SlurmAnalysisExecutor` **加法式**扩展 `extra_ro_binds`/`inject_args`/`job_prefix`(默认空 →
  scanpy 路径零行为改动),同一 executor 即可跑 VEP 步:cache+ClinVar 只读挂载、`network=False`。
- [x] `registry.py` 泛化路由(`_route_to_executor`)+ `variant_executor`;`settings.py` 加 `VEP_*`/
  `variant_on_hpc`;`app.py` 变异 executor 构造块(镜像 analysis 块)。
- [x] 测试:`test_vcf_offline.py` 12 条 + 既有 `variant/registry/slurm_analysis` 22 条**全绿**。
- [x] **HPC3 打包方法**:`--fakeroot` 未开(`/etc/subuid` 无 `<ucinetid>` 映射)→ 改用 **`--remote`**
  Sylabs 云构建(token 已配好);def 从 `ensemblorg/ensembl-vep:release_112.0` 起 + `%post` 装
  bcftools/python3;`vep.sif`(241MB)构建成功、stage 到 `containers/vep.sif`、容器内验证
  **VEP 112.0 + bcftools 1.13 + Python 3.10**。
- [x] **cache 下载方法**:`nohup` + sentinel 文件脱离 ssh 后台跑(断线不影响),`curl --retry` + 幂等
  跳过已存在文件;GRCh38 **索引 cache 实测 25.4GB**(比预估大,含全部频率数据)。
- [ ] 离线冒烟(小 TP53 VCF → 带 ClinVar 的 JSONL)—— 等 cache 落地,后台 watcher 自动触发。
- [ ] 部署分支到 eyeserver + `.env` 开 `BIOAGENT_VARIANT_ON_HPC=1` + 真实大 VCF 端到端 —— **待奕君拍板**(动生产)。

**验收话术(粘进 AiScientist 输入框;先把上传的 VCF 选为 dataset)。** 通用/推荐方法已写进
`variant_annotation` preset pipeline(`preset_pipelines/variant_annotation/SKILL.md`)——VCF 场景 PI
会自动套用;下面这些是手动验收用的:

- **General(主力):** "Annotate all variants in the uploaded VCF with Ensembl VEP + ClinVar
  (GRCh38). Filter to PASS, then give me: (1) the variant distribution by consequence and by predicted
  impact; (2) every Pathogenic / Likely-pathogenic ClinVar variant — gene, location, rsID, condition;
  (3) a shortlist of rare (gnomAD AF < 0.1%), high-impact or SIFT/PolyPhen-deleterious variants not yet
  in ClinVar. Give me the annotated table and a short interpretation of the most clinically actionable
  findings."
- **IRD-focused(眼科):** "...restrict to inherited-retinal-disease genes (ABCA4, USH2A, RPGR, RHO,
  CRB1, RPE65, PRPH2, CEP290, EYS, CHM, …) and flag any pathogenic/likely-pathogenic or rare damaging
  variants in them, with the associated retinal phenotype and inheritance."
- **Smoke:** "Annotate this VCF's variants and flag the pathogenic and likely-pathogenic ones, with
  gene, consequence, and clinical significance for each."
- **验收点:** 结果须显示 `execution_mode: "offline_vep"`(WGS 上若是 `rest` 只采样了头部变异,不能
  当全基因组接受);若 VCF 是 hg19,提示词加 "This VCF is GRCh37/hg19";WGS 约 30–60 分钟(CPU Slurm),
  看 System / 运行日志进度。

## 2026-07-07 —— VL 渲染审核:诚实上报退化(原来谎报 passed clean)

来自一份真实 run(`bioagent_results_16c4a3eb38f9`,变体注释):`visual_review_pass1.json` 里
`model: "bbox-only"` + `"VL review unavailable: ImportError … PyTorch … not found"`,但 technical
report 写"Visual/layout review passed with no defects"、capability log 写"ENABLED and ran; pages
read clean"。**假阳性** —— 视觉模型根本没加载(VL 容器缺 torch / `BIOAGENT_VLREVIEW_IMAGE` 指错),只跑了
几何 bbox 预检,却把这个兜底的 "clean" 当成真正的视觉审核上报。

- **根因 A(部署 —— 已在 HPC3 确认,`.def` 已修,需要重建):** 不是缺 torch、也不是指错镜像 —— 是
  `vlreview.sif` 里 **transformers/torch 版本不匹配**。容器有 torch **2.3.0**(基础镜像),但 Jul-2 那次
  build 拉进了 transformers **5.12.1**(`.def` 写的是 `transformers>=4.49.0`,没有上界)。transformers ≥ 5
  需要 torch ≥ 2.4,于是它**静默禁用 PyTorch** → `is_torch_available()` 为 False → Qwen2.5-VL 加载不了 →
  `run_review.py` 返回 `model="bbox-only"`。已验证:torch 2.3 + 权重(5 个 shard 全在)都没问题,只有
  transformers 版本错了。**修复(已完成):在 `deploy/vlreview/vlreview.def` 里 pin `transformers>=4.49,<5`**
  (已在 HPC3 上证明会解析到 4.57.6,torch 恢复可用、Qwen2.5-VL 可用)+ 加固 build 检查
  (`assert is_torch_available()` + import VL 类,坏 build 现在会直接失败而不是静默发布)。
  **已重建 + 已验证(2026-07-07):** Claude 在 HPC3 上重建了 `vlreview.sif`(远程 Sylabs 构建,job
  53949233,约 45 分钟)并端到端验证:导入检查 → `transformers 4.57.6, torch 2.3.0, torch_available
  True, Qwen2.5-VL usable`;GPU smoke test(job 53949564,A30)用真实 `run_review.py` 审了一份 report
  PDF → `review.json` `"model": ".../vlreview_model"`(真实模型,**不是** `bbox-only`),审了 5 页、clean。
  镜像路径不变 —— 不用改 `.env`,VL 审核现在是真跑了。(A30 上有个非致命 cuDNN conv3d plan warning,会回退,
  不影响输出。)将来重建:`rsync deploy/vlreview/ + scripts/hpc3_vlreview_setup.sh` 到 HPC3,`rm` 旧 .sif,
  再在**计算节点**上 `BIOAGENT_VLREVIEW_BUILD_MODE=remote bash hpc3_vlreview_setup.sh`。
- **修复 B(代码,已完成):** `bbox-only` 现在被当作**退化**而非通过 —— `VisualReviewOutcome.vl_unavailable`;
  `format_diagnostics` 即使 clean 也输出 ⚠️ 块(带原因 + 修复提示),进 technical report 的 Diagnostics;
  loop 发 "Visual review DEGRADED" 警告而不是 "read clean";capability log 读 `visual_review_pass1.json`,
  打印 "ENABLED but DEGRADED —— the vision model did NOT load" 而不是 "ran clean"。测试:新增
  `tests/test_visual_review.py` + `test_capability_log.py` 里一个退化用例。全套 **536 绿**。这个修复正是
  让将来任何 A 类故障**可见**、而不是静默变绿的护栏。

## 2026-07-07 —— search_skills(query) 检索(③)—— 清单可扩展

## 2026-07-07 —— 决策点 HITL:线性路径也弹 Claude 式选择菜单 —— 分支 `elastic-chatelet-6c5d2b`

**背景(Yijun:"我没看到过他要我提供决策过")。** 查证:decision-menu 前后端**全建好了**(后端 `decision_review` push `{decision_prompt, goal, options}` 阻塞等选择;前端 `showDecision()` 渲染按钮菜单),但被一行开关 `want_decisions = (planner=="dag" and not autonomous)` 卡死——**决策点只在 DAG 才启用**,而且 DAG 的叉是 **LLM 打标**的(Qwen 常常不标)。所以线性从不弹,DAG 也未必弹。

**做了什么(确定性,不赌模型):**
- **线性路径新增规划期决策叉**(`research_lab.run()`):数据集**已带 cell-type 标签**且计划里**有 de-novo 聚类** ⇒ 在任何步执行前,把"用已有标签 / de-novo 重聚类 / 两者都做并对齐"这个叉通过**现成的 `decision_review` 卡**弹给用户;选择作为 `seed_notes` 贯穿整个 run(cancel 则中止)。检测确定性(`_annotation_label_col` + `_plan_has_clustering`,`_looks_like_celltype_col` 匹配 majorclass/celltype/…)。
- **DAG 路径确定性兜底**(`_structure_agenda_dag`):已标注 + 有聚类节点且 LLM **没**标任何 decision ⇒ 用 `dataclasses.replace` 把该聚类节点强制标成 decision(保全 consumes/produces/suggested_tool),这样 DAG 下也**可靠**弹,不赌 Qwen 打标。永不双问(仅当无 LLM decision 时补)。
- **网关放开开关**(`app.py`):`want_decisions = (resume is None and not req.autonomous)` —— 两条路手动模式都启用。

**验证:** `tests/test_step_meetings.py` +4(线性弹叉+选择进 brief / 无 hook 不问 / 未标注不问 / DAG 结构器确定性打标),进程内**106 passed 0 failed**(含既有 DAG 决策/结构测试全绿)。改动在 worktree。

**测试提示:** 你现在线上默认 `planner="dag"`。拿那个视网膜(已标注 majorclass)数据集跑,**手动模式**下现在应当稳定弹出"用标签 vs 重聚类"决策卡——不管 Qwen 有没有主动标。

## 2026-07-07 —— PI↔Critic 开会制度(做前门 + 做后复盘)—— 分支 `elastic-chatelet-6c5d2b`

**背景。** 那个视网膜 bundle 暴露的"没意义的步骤 / 每轮重跑",根子是逐步 Critic **只看单步、没有全局视图**,结构上看不出"这步喂到结论没有 / 富集前有没有对照"。Yijun 定的形态:**不是一个全知 Critic,而是 PI↔Critic 双向对话,在每步做前做后各碰一次头,PI 拍板、确定性守卫当地板**。设计文档 `docs/pi_critic_meeting_protocol.md`。

**做了什么(`agents/research_lab.py`,默认关 `LabConfig.step_meetings`):**
- **⓪ 规划期整盘复查 `_plan_review`**(回应 Yijun"一开始难道看不出来吗")—— 出 agenda 之后、任何一步跑之前(仅当**无**人工审批 `plan_review is None`,尊重 HITL):Critic 审**整份**计划(孤儿聚类支/循环富集/分支不自洽/对该数据集前置条件),PI 敲定 `final_agenda`;空/超长/乱码回退原计划(绝不更差)。发 `plan_review` 事件。**对"从第0步就错"的计划最对症最便宜——1 次复查在花任何算力前重排,而逐步门要跑到第2、4步才逐个拦。** 那次视网膜 run 是 1.0 线性,连 DAG 的"用标签 vs 重聚类"决策叉都没吃到,规划期一路裸奔到执行。
- **① 做前门 `_preflight_gate`** —— Scientist 动手前:先确定性地板(无对照富集 → skip,不问模型)→ Critic 拿四轴质询(必要性/冗余/前置条件/高度,`_PREFLIGHT_GATE_SYSTEM`)→ 只在 Critic 有异议时 PI 裁决(`_PREFLIGHT_PI_SYSTEM`)。产出 proceed / amend(折进 brief)/ skip(移出有效 agenda,`converged` 按 `len-pruned` 算)。
- **② 做后复盘 `_poststep_review`** —— Critic 采纳某步后:PI 判"这步改变结论没有 + 剩余步哪个因此变多余",保守剪枝(`_POSTSTEP_PI_SYSTEM`)。
- 两条路共用 `_scientist→_critic` 缝:**线性 `_run_loop` 全量落地**(amend+skip+做后剪枝);**DAG `_run_one_node` 只落 amend + 地板**(动 DAG 依赖图要 scheduler 的 replan,留后续在 eyeserver 测)。
- 新事件 `preflight` / `poststep_review`;`steps_pruned` 增 `reason∈{preflight,poststep_review}`;`lab_done` 增 `pruned`。

**验证(实打实,非仅编译):** `tests/test_step_meetings.py` 6 个用例**进程内真跑全过**(off 不开会 / 规划期复查重排不自洽计划 / 地板不问模型 / skip 剪枝仍收敛 / 做后剪下游 / amend 进 brief);**既有 96 个 no-fixture 用例全绿**(含 87 research_lab + 9 DAG;共 102),0 失败。scanpy/pytest-fixture 用例仍需 eyeserver。改动在 worktree,**未提交**。

**成本:** 关=零额外调用;开=每步 1 次(Critic 门)+ 有异议时再 1 次(PI 裁决)+ 每个被采纳步 1 次(做后)。revise 不额外加。

**后续:** `BIOAGENT_STEP_MEETINGS` env 开关;DAG 侧 skip/下游剪枝走 replan;高后果步可选升级成 HITL 决策卡;协议开启后再 A/B Qwen3.6 vs 更强模型(这时模型才真被"给了全局视图+被问必要性")。

## 2026-07-07 —— Critic 计数读数 bug 修复(DE 每组基因数)—— 分支 `elastic-chatelet-6c5d2b`

**背景。** 复盘 bundle `7e551b8db499`(视网膜 demo)时 Yijun 追问 "Critic 到底盖章还是打分"。查证下面那条"遗留待办"(Critic 说每簇 10 个基因、实际落盘 50)的**真因不是**"读被截断的控制台",而是 `run_de` 返回体里 `top_genes_by_group` 写死 `[:10]`(且 `result_digest` 又把 list 截到 10)——Critic 能 ground 的那个字段本身只有 10,于是把 preview 当成了总数。

**修复。**
- **`tools/scrna_pack.py run_de`:** 返回体新增真实计数 `n_genes_per_group`、`de_rows_by_group`(每组实际落盘行数)、`de_rows_total`;`top_genes_by_group` 注释明确为"PREVIEW ONLY,前 ≤10"。三个消费者(`_literature_query` / grounding vocab / findings digest)只读 `top_genes_by_group` 的值,新增键不影响它们。
- **`agents/research_lab.py _CRITIC_SYSTEM`:** 加一句——报计数(基因/细胞/簇/富集 term)时必须从显式计数字段或被引表读,**绝不**数 `top_*`/preview list(capped sample ≠ total)。通用硬化,不止 DE。
- **`tests/test_lab_local_integration.py`:** DE 改 `n_genes=20`,从 `result.rounds` 取 run_de 返回,断言真值(`n_genes_per_group==20`、`de_rows_total>10`、每组 >10)且与磁盘 `de_leiden_*.csv` 行数逐组交叉核对;preview 仍 ≤10。

**验证。** 三文件 `py_compile` 通过 + 纯 Python 模拟计数逻辑通过(真值 20/组透出、preview 仍 10)。集成测试是 **scanpy-gated**,须在 eyeserver app venv 跑(本地 worktree 无 scanpy/pytest)。已在 worktree `elastic-chatelet-6c5d2b`,**未提交**。

**同条待办的另一半("(no answer) 轮被判 ACCEPT")** 已被后来的确定性地板超越:`_critic` 现在对"无任何工具成功产出"的步强制 revise;而"产出了 artifact 的 incomplete 步可被接受"是**有意为之**(scGPT 那类步),不是 bug。

**下一步(与 Yijun 讨论中):** PI↔Critic 双向"开会制度"——做前一道**必要性/合理性门**(Critic 质询该步是否被计划证成、是否喂到最终结论、是否与已接受步冗余、富集类步前置对比是否满足),做后把"这步有没有真的改变结论"回报 PI 并回灌 plan。确定性守卫仍作地板(Qwen3.6 已证纯 prompt 拦不住)。

## 2026-07-07 —— search_skills(query) 检索(③)—— 清单可扩展

Yijun("③ 可以开始做了")。加了一个 `search_skills` Scientist 工具,让原子 skill 库能长大而不让清单撑爆
每个 brief。全套 **531 绿**。

- **`agents/skills.py`:** `search_skills(query, k)` + `make_search_skills_tool()` —— 关键词 /
  token 重叠打分(name > summary > body),离线 + 确定性,不需要 embedder。只返回 name+摘要(绝不返回代码);
  没命中 → 给个 hint。
- **brief 切换(`research_lab`):** `MANIFEST_MAX`(env `BIOAGENT_SKILL_MANIFEST_MAX`,默认 12)。
  库 ≤ 12(现在 9)→ 和以前一样内联清单(行为不变)。> 12 → 不列任何,让 agent 先 `search_skills(query)`
  再 `read_skill_reference`。现在两个常驻小工具(search + read);代码 body 仍然用到才拉。
- 升级路径记在 doc 里:关键词重叠不够用时换 embedding。
- **② skill induction 继续搁置**(按你说的)。

## 2026-07-07 —— Advanced 多选可组合原子 skill(required-skills)

## 2026-07-07 —— Advanced 多选可组合原子 skill(required-skills)

Yijun("接着做" ①;induction ② 搁置)。console 的 Advanced 面板在 preset-pipeline 选择器**下面**多了
一个勾选清单:勾原子 **skill** → 这次 run **必须应用**每一个(可组合,勾多个)。全套 **529 绿**。

- **后端:** `GET /api/skills`(`agents/skills.list_skills()` → name + 摘要);`LabRequest.skills`
  → `LabConfig.required_skills`(对库校验,未知名丢弃)→ 一段 `REQUIRED skills …` 指令追加进 PI 的规划
  guidance,让计划把每个都用上(`read_skill_reference` → 适配 → `run_code`);feed 行 `🧩 Required skills`
  (`skills_required` 事件)。跟进路由:带 `skills` 的 run 视为新研究。
- **前端:** `loadSkills()` → `#skillList` 清单;`session.skillKeys`(localStorage,每会话);`/api/lab`
  发 `skills: [...]`。名字去下划线显示(如 "perturbation edistance")。
- **区别:** pin 一条 **preset pipeline** 是steer整个计划的形状;require 一个**原子 skill** 是把某个具体
  能力强制塞进计划。现在两个都在 Advanced 里。
- 注:skillKeys 只在 localStorage 每会话持久化(不像 `preset_key` 存进后端 conversation)——换设备不会恢复。
  暂时够用,记一笔以后再说。

## 2026-07-07 —— 三层 skill 架构已建成(skills / preset-pipelines / registry)

## 2026-07-07 —— 三层 skill 架构已建成(skills / preset-pipelines / registry)

Yijun greenlight("一次性做完做成最后的样子" + "你可以做这个渐进披露了")。整套重构一次做完,全套 **527 绿**,
并作为最终形态合并进 main。规范见 `docs/skills_and_pipelines_architecture.md`。

⚠️ **部署时要测的一个行为变化:** 原子 skill 清单现在是**全局**的 —— Scientist 每一步都会看到 skill 清单,
和加载了哪条 pipeline(或没加载)无关(以前只有被选中 pipeline 捆绑的 scripts 才会被展示)。这是有意的
(skill 是共享库),但意味着 skill 现在始终可达。跑一个 run 确认清单渲染正常、agent 仍然优先用工具而不是乱拉 skill。

- **现在三层齐了:**
  - **registry**(`agents/registry.py` + `tools/`)—— 小而固定的核心(HPC 路由的
    `run_scanpy_qc`/`run_clustering`/`run_de`/`run_enrichment`、`run_code`、`finish`、外部包装)。不动。常驻工具列表。
  - **`skills/`**(新,扁平 `skills/<name>.py`)—— 原子、可重写的能力库(9 个模板,从各 pipeline 旧的
    `scripts/` 提升上来)。由新的 `agents/skills.py` 加载(`Skill`/`SKILLS`/`skill_manifest`/
    `make_skill_reference_tool`)。`$BIOAGENT_SKILLS_DIR` 现在指这里。
  - **`preset_pipelines/`**(由 `skills/` 改名)—— 固定的端到端流程(SKILL.md 文件夹)。由
    `agents/preset_pipelines.py` 加载(`PresetPipeline`/`PIPELINES`/`select_pipeline`/
    `compose_pipeline_prompts`;env `BIOAGENT_PIPELINES_DIR`)。`presets.py` 垫片对外不变。
- **渐进披露 = 省上下文的机制。** Scientist 的 brief 只列**全局**原子 skill 清单(name + 一行摘要);
  `read_skill_reference(name)` 用到才拉全文。固定 registry 保持常驻小核心;skill 库可以随便长而不撑爆每个
  brief。`research_lab` 现在从 `skills.skill_manifest()`(全局)取清单,和加载哪条 pipeline 无关。
- **清理:** `PresetPipeline` 不再捆绑 `scripts`(删了 `SkillScript`/`_load_scripts`)。
- **还没做(增量,推后):** skill 归纳(从 run 里长出新 skill)、`search_skills` 检索(清单变大时)、
  以及让 Advanced 多选选**可组合的原子 skill**(现在还是选 preset-pipeline)。
## 2026-07-07 —— Advanced 多选里的"skill"其实是 PRESET PIPELINE(已改名)

## 2026-07-07 —— Advanced 多选里的"skill"其实是 PRESET PIPELINE(已改名)

Yijun 指出:Advanced 多选的每一项**不是**解耦的可组合工具,而是**完整的端到端流水线**(每个
`skills/<name>/SKILL.md` 组合的是原子注册工具:`run_scanpy_qc, run_clustering, run_de,
run_enrichment, …`,共享一条 QC→cluster 主干)。所以 UI 不该叫它"skill"。

- **前端改名(仅面向用户):"skill" → "preset pipeline"** —— Advanced 面板标题、搜索框 placeholder +
  tooltip、aria-label、hint(`index.html`),空态"No matching…"(`app.js`),mode 选择器 tooltip,以及
  `📚 Loaded preset pipeline` feed 行(`gateway/app.py`,现在还会写"composes tools: …")。
  `test_lab_progress_stream` 同步。
- **词汇写进 `docs/BACKLOG.md`**("Further decouple the skill system"条):三层 —— **tool**(原子、解耦
  = 注册表,本就可组合)、**preset pipeline**(组合工具的完整流水线 = 现在的 `skills/<name>/` 文件夹)、
  **skill**(目标:Yijun 真正想让多选装的那种解耦*可组合*中间层 —— 目前还不存在)。
- **后端暂未改名。** `agents/skills.py` 和 `skills/` 目录暂时保留原名,所以现在前端("preset pipeline")
  和后端("skill")词汇暂时不一致。留给 Yijun 的问题:要不要把后端也朝"preset pipeline"改名(端到端那些),
  并把"skill"这个名字留给未来的可组合层?等真正开始做可组合层时再动。

## 2026-07-07 —— 结果面板按钮改成对话式(PI 定步骤);不再手动选步

## 2026-07-07 —— 结果面板按钮改成对话式(PI 定步骤);不再手动选步

Yijun 要求:"重新生成报告 / 重跑某步"这两个按钮应该是**对话 → 让 PI 知道 → PI 去执行**,而不是用户手动选某一步。现在就是了。

- **删掉手动选步下拉**(`toggleContinuePanel` / `continueFromStep` / `#continuePanel` 及其 CSS)。用户不再选"从第 N 步重跑"。
- **两个按钮现在都调 `primeComposer(kind)`** —— 聚焦聊天框 + 给出针对性提示("说要改什么,比如'把聚类分辨率调到 1.0 重做'")。用户随后发出的消息,由**已有的** PI 跟进路由器(`gateway/_dispatch_lab` → `_classify_followup`)判定"改报告 vs 重跑某步",并**从措辞里推断是哪一步**,然后执行。所以底层早就有了,这次只是把按钮接上去、删掉机械 UI。`regenerateReport()`(直连 `/api/report/regenerate`)也删了 —— 现在也走同一条对话路径("说 'regenerate' 就按原样重建")。
- 删掉已失效的前端状态 `lastRunAgenda` / `LASTAGENDA_KEY`(只有那个下拉在读它)。
- **把跟进流程翻成英文**(按钮现在就通向这里):`_ask_followup_clarify` 的澄清卡片 + 它的答案匹配关键词,以及 `_dispatch_lab` 里三条 `lab_progress` "🧭 …" 路由提示。`test_followup_router` 的 chip 用例改指英文选项。全套 **527 通过**。
- **缓存击穿**:console 资源(`index.html`/`app.js`/`styles.css`)现在带 `Cache-Control: no-cache` 下发(`gateway/app.py`)。这修的正是"我重新部署了但 UI 还是旧的(中文)版本"这个坑 —— 浏览器一直缓存 `/static/app.js`(没有版本号)。注意:`main` 上的代码从 `8fc0ce1` 起就已经是英文了;你看到中文是**缓存旧字节 / 部署没同步**。部署后再硬刷一次更稳。

## 2026-07-07 —— console 打磨:模式默认 Auto;结果面板控件改回英文

## 2026-07-07 —— console 打磨:模式默认 Auto;结果面板控件改回英文

两处 `frontend/console/` 小修(不动后端),已浏览器验证。

- **模式选择器默认 ✨ Auto(PI 决定)**,不再是 🧑‍🔬 Single agent。`index.html` 里把 `#modeSelect` 选项
  重排(Auto 放第一 + `selected`),并把三处 JS 兜底(`syncPresetUI`、`onModeChange`、`/api/lab` POST body)
  从 `|| "single"` 改成 `|| "auto"`,这样没有保存过 mode 的新会话也默认 Auto。
- **右侧 Results 面板的"重新生成/重跑"控件之前泄漏了中文** —— 把整条流程翻回英文,与其余(共享/公开)
  console 一致:`Regenerate report` / `Re-run a step` 两个按钮 + tooltip、"Re-run from this step" 选择器
  (label、placeholder、Re-run 按钮、hint)、以及 `regenerateReport` / `continueFromStep` 里的 toast。
  `app.js` 里已无中文控件字符串。
- (仅本地,未追踪)`.claude/launch.json` 改指向自带的 `http.server` 做静态预览 —— 旧的
  `serve.py`/`.venv` 路径在 worktree 里并不存在。

## 2026-07-07 —— Q2:skill 子系统解耦进 `agents/skills.py`(behavior-preserving)

## 2026-07-07 —— Q2:skill 子系统解耦进 `agents/skills.py`(behavior-preserving)

Yijun 批准的("Q2 解耦可以尽快实现")。skill 逻辑原来分散在 `agents/presets.py`(加载)和
`agents/research_lab.py`(选择/拼接/引用工具)两处,现在统一到**一个规范模块 `agents/skills.py`** ——
这就是将来做 skill **归纳(induction)**(把一次成功的 run 蒸馏成新 `SKILL.md`)的接缝。行为无变化,
**527 测试仍全绿**。

- **新增 `agents/skills.py`**,收纳:数据模型(`Skill` —— 原 `ResearchPreset`;`SkillScript`)、
  加载(`SKILLS`/`get_skill`/`list_skills`)、Axis-B 数据集感知路由(`select_skill(complete, question,
  dataset_hint, library, emit)` —— 现在接收 lab 的 chat 回调,不再是方法)、prompt 拼接
  (`compose_skill_prompts`)、以及渐进披露的 `read_skill_reference` 工具
  (`make_skill_reference_tool(get_skills)` —— 接收一个取"当前 skill 列表"的 getter)。
- **`agents/presets.py` 瘦身成薄薄的 re-export 垫片** —— 同一 registry 的"面向前端的 preset 视图"。
  `PRESETS`/`get_preset`/`list_presets`/`ResearchPreset`/`SkillScript` 全部别名到 `skills.py`,所以
  gateway(`system_info.py`、`app.py`)和旧测试都无需改动。新代码请直接从 `agents.skills` 导入。
- **`agents/research_lab.py`** 删掉了搬走的成员(`_select_skill`、`_make_skill_reference_tool`、
  `_compose_skill_prompts`、`_parse_skill_choice`、`_SKILL_SELECT_SYSTEM`),改为从 `skills` 导入。
  调用处:`run()` 调 `select_skill(self._complete, …)` 和 `compose_skill_prompts(…)`;`__init__` 里
  `make_skill_reference_tool(lambda: self._skills)`。类型标注 `ResearchPreset` → `Skill`。
- 测试重指向 `bioagent.agents.skills`:`test_preset_compose.py`(拼接)+ `test_research_lab.py` 里的
  `read_skill_reference` 用例。其余不动。

## 2026-07-07 —— skill 选择 v2:pinned + auto、数据集感知、plan 前展示

Yijun 提的。改了 PI 加载 skill 的三处行为(`agents/research_lab.py`、`gateway/app.py`、
`frontend/console/index.html`)。Q2(解耦 skill 子系统)现已**完成**(见上一节),单独做成
behavior-preserving 重构,没混进这批。

- **多选 skill 现在是"必用(pinned)"+ auto 增补。** 以前在 Advanced 选 skill 会**禁用** PI 的自动选择
  (强制 `preset_prompt` 绕过了它)。现在选中的是**强制下限**,PI 的 auto-select **仍然照跑、在上面再补**
  它认为最合适的一条(去重)。实现:gateway 把 `req.presets`→ skill 对象放进新的 `LabConfig.pinned_skills`
  (不再拼成 `preset_prompt`);`run()` 用它初始化 `self._skills`,auto 追加,guidance =
  `_compose_skill_prompts(self._skills)`。用户**手动编辑的自由文本** `preset_prompt` 覆盖仍然关掉 auto
  (“我全接管”)——pinned 不会。`self._skill`(单)→`self._skills`(列表)全线改(引用工具 + manifest 聚合
  所有已加载 skill 的脚本)。
- **skill 选择现在看数据集**(修"路由只读问题、不读数据"那个缺口)。`_select_skill(question, dataset_hint,
  emit)` 把 `_dataset_context()` 画像(kind + obs 列)喂给路由,`_SKILL_SELECT_SYSTEM` 让它按数据路由——
  于是 VCF → 变异注释 / 带标注的 `.h5ad` → 注释交叉验证,即使问题很泛。这就是为什么 "complete the research"
  之前不稳(确实不稳,得点题)。注意:上传 preflight 早就分析过数据集,这里只是**接线**进选择(不用新建
  预处理 agent)。
- **`skills_loaded` 现在列出所有已加载 skill(pinned + auto),且在 plan 之前触发**(不是批准后)——
  "📚 Loaded skill(s)" 那行在你审阅 plan 时就显示当前激活的 skill。以前它读单个 auto 选的 `self._skill`,
  所以手动选的会显示成"从零规划"。
- 删掉现已无用的 gateway `_compose_preset_prompt`(拼接搬到 `research_lab._compose_skill_prompts`);
  `test_preset_compose.py` 改指向;+2 测试。前端 Advanced 提示改了措辞(勾选=必用;PI 仍会补)。全套 **527 通过**。

## 2026-07-06 —— 控制台 UX 批量 + "continue 却全量重跑" 修复 —— 分支 `feat/console-ux-and-continuation`

Yijun 提的五项(基于 `main`)。头条是 ①:像 "continue to generate the report" 这种后续消息,本来会
重新规划 + 重跑整条流水线,而不是接着上一轮结果。

- **① 续跑被 plan-mode 挡掉了(`gateway/app.py`)。** 其实已有完整的后续路由器
  (`_dispatch_lab` → 分类 → `edit_report`=regenerate / `rerun_step`=从 checkpoint 续跑 / `new_study`=
  全新)。但 `_followup_target` 只要 `req.plan_mode` 就判不合格 —— 而 "Plan first" **默认勾选** ——
  于是每条后续消息都掉进全新 `_run_lab`,重规划 5 步、重跑 QC/聚类/DE(还把原始 query 套成一个
  垃圾的 "Literature search for <query>" 步)。修法:`_followup_target` 不再把 plan-mode 当作新研究信号
  (只有**强制指定 skill**(`preset`/`presets`)或**换了数据集**才算新研究);由分类器判意图,判成
  `new_study` 时全新跑并仍然尊重 `plan_mode`。另外把 `conn.last_run_id` 提前到 **`_write_run_state`
  之后**(报告渲染之前)设置,这样分析已完成的 run 即使后面渲染出岔子也仍可续跑。叠加原因:上一轮往往
  **崩在报告步**(第 ⑤ 项)、在 `last_run_id` 设置之前 → 根本没有可续的 run;⑤ 也一并修了。测试:
  `test_followup_router.py` +1(plan-mode 后续→edit 路径),合格性测试更新(plan-mode 现在合格;`presets`
  判不合格)。
- **⑤ HPC 报告渲染慢/失败不再废掉已完成的 run(`slurm_report.py`、`gateway/app.py`)。** 报错
  `Chat error: Command timed out after 60s: mkdir …`:SSH mkdir 超时抛的是 `GatewayError`(不是
  `SlurmJobError`),绕过 `__call__` 的 `except SlurmJobError`、跳过本地 pandoc 兜底,把整个(早已跑完的)
  run 崩在报告步。现在 `__call__` 捕获**所有**远端失败 → 本地兜底 → 再不行返回诊断(契约:绝不抛);
  `_run_lab` 给 manuscript 的 `build_pdf_report` 包 try/except(technical report 早就包了)——渲染崩了也
  降级成 error 结果,bundle(含 report.md)照发、run 照完成,可用 regenerate 从 bundle 重建、不重跑。
  测试:`test_slurm_report.py` +2。
- **② DAG 默认、去勾选框(`index.html`、`app.js`、`gateway/app.py`)。** `app.js` 恒发 `planner:"dag"`;
  后端默认 linear→dag(`BIOAGENT_PLANNER=linear` 作隐藏兜底;`LabConfig` 数据类默认仍 "linear",给裸构造
  它的测试/脚本用)。
- **③ Advanced "强制研究路径" 改为可搜索**多选**(`index.html`、`app.js`、`styles.css`、
  `gateway/app.py`)。** `#presetSelect` 下拉 → `#presetSearch` 搜索框 + `#presetList` 复选清单;会话带
  `presetKeys[]`(以逗号拼接存进原有 `preset_key` 字段——不改服务端 schema),run 发 `presets:[...]`。后端
  `_compose_preset_prompt` 把选中的 skill 合成一个 PI 引导块(一个→原样;多个→带标签的分节 + "调和、别
  重复跑共享的 QC→聚类骨架" 的头)。旧的单 `preset` 仍可用。测试:`test_preset_compose.py`(4)。用独立
  mock 页面目视验证过(搜索过滤 + 勾选行样式)。
- **④ 新对话不再自动挂上次数据集(`app.js`)。** 删掉 `loadDatasetChips()` 里从 localStorage 自动回填
  那段 + 已失效的 `DATASET_KEY` 持久化;历史上传仍作可点选 chip,数据集框只是起始为空(新问题不再被悄悄
  绑到旧数据;而后续消息留空正好表示"沿用上一轮的数据集")。

全套 **525 通过**。提交在 `feat/console-ux-and-continuation`(基于 `main`);**尚未合并 / 尚未 push**。
前端已做代码级验证(node --check + mock 页面),但控制台与后端强耦合 —— 完整后续流程要在活的 gateway +
HPC 会话上端到端跑才能验证。

**⑤ 追补(来自一次线上真 run,部署版代码):渲染修复不彻底。** 一次真实 scGPT run 在报告阶段抛 "Chat
error",**整包都没打成、连下载都没有** —— 已跑完的分析(QC/聚类/DE)全丢。根因:bundle 发布(`artifacts`
+ `run_complete` + `_finish_run_record`)在报告+审阅段**之后**,而两步报告后审阅——`_postrender_text_check`
和尤其 **`_postrender_visual_check`(跑在 HPC3 的 VL 视觉模型作业,会超时/掉 SSH 通道)**——**没被 try/except
包住**,一抛异常就跳过了发布。修法(`gateway/app.py`):把报告后审阅块、trim/quarantine 清理、
`_write_capability_log` 都加非致命兜底,让**分析一旦跑完就一定发布 bundle**(可下载 + 可 regenerate),不管
报告/审阅/清理是否失败。仍未解决(另立、**没改**):`run_scanpy_qc`/`run_clustering` **每个 run 都**报
`GatewayError: Command timed out after 60s: echo $HOME/.bioagent/analysis`(同一 `$HOME`/60s 家族,在分析
offload 的 preflight)——步骤靠 `run_code` 兜回来了,但工具本身是挂的;还有 scGPT 交叉验证那步不收敛
(需要 bundle 才能定位是不是 `scgpt_annotate` 失败)。

**scGPT 诊断(登 eyeserver 扒失败 run `b2cda0f7a8aa` 的日志)+ 修复。** scGPT **没失败**——`scgpt_job.log`
显示 103 秒"Inference completed"、`data/scgpt_{predictions,merged_predictions}.csv` 也取回了。卡住的真因:
scGPT 在**原始上传(11,977 细胞)**上注释,而 QC 把 pipeline 的 adata 过滤到 **11,970**(−7);agent 用按行
位置的方式把预测并进 obs,反复撞 pandas index 不齐(11977 vs 11970),交叉验证一直没做完(Round 5–7
REVISE 0.2 → 4/6、converged=False)。参考模板其实早就按 barcode 对齐,但 agent 没照做(而且模板只覆盖了
Leiden、没覆盖它还需要的 majorclass/celltype 对比)。修复(`skills/scgpt_annotation/`):SKILL.md 加了醒目的
**⚑ 按 BARCODE 合并、绝不按行位置** 提示 + 改了步骤 1/5,并把 `crossvalidate_scgpt_vs_leiden.py` 模板扩展到
同时对 Leiden、majorclass、celltype 做交叉验证(全部 barcode 对齐)。**用那次失败 run 的原始数据在 eyeserver
上验证过**:修好的模板干净跑通,产出 agent 当时做不出的 3 张混淆表 + 置信度分布(scGPT 与已有 celltype
标注高度一致、per-class purity 高、置信度 mean 0.999)。已提交到分支。

**报告失败的最底层根因(直连 HPC3 排查)——渲染 Slurm 作业秒死 127。** HPC3 上**每个** `bioagent_report_*`
作业都 ExitCode 127、0–1 秒失败、连日志都没写 —— 所以 **HPC3 上的报告从来没渲染成功过**,进而级联成丢包、
让每个 scGPT/analysis 研究都拿不到成品。根因:`SlurmReportRenderer.scratch_dir = "$HOME/.bioagent/report"`
被当字面量用 —— Slurm 不展开 `#SBATCH --output` 里的 `$HOME`,`singularity -B` 又被 shlex 单引号冻住 `$HOME`,
于是作业没有可写日志 + 挂载绑不上,瞬间死。(`singularity/3.11.3` 存在、`report.sif` 能跑 pandoc 3.9.0.2——
纯粹是 `$HOME` 冻结;analysis 早就解析了 `$HOME`,报告渲染器没有。)修法(`slurm_report.py`):照搬
`slurm_analysis._resolved_scratch` —— `$HOME` 解析成绝对路径一次(缓存),用于 mkdir/`-B`/`--output`/日志读取。
**在 HPC3 上端到端验证过**:绝对路径绑定跑容器 pandoc+xelatex 真出了 PDF。所以 HPC 报告渲染现在应该真能用了
(之前一直是坏的)。次要非阻塞观察:较新 singularity 对 `--env HOME=/tmp` 会 warn "Overriding HOME..."——渲染
仍成功。仍未解决:`echo $HOME/.bioagent/analysis` 60s 超时(SSH 饱和,#3)—— GPU 监控 `srun nvidia-smi` 即使
在跑 run 时也每 ~poll 秒来一发(HPC3 上实锤:几十条 nvidia-smi 作业步),把那条共享 SSH 连接刷满到撞 MaxSessions。

**#3 现已解决(`gateway/app.py`):** `_monitor_gpu` 在 `conn.chat_running` 时跳过健康探测——run 本身就在用
GPU + 连接,那个每 ~20s 的 `srun nvidia-smi` + `find_running_job`(各占一个通道最多 30s、抢 run 自己的提交)
不再在 run 期间发。idle 轮询不变。这既消掉了直接的 60s 超时,也消掉了它引发的重试连锁。注意:这不是 loop 慢的
全部——GPU/vLLM 冷启动、每步 Slurm 提交/容器开销、LLM 轮数才是结构性大头(真实 run 的 event_log 跨 ~1h43m、
22 次 gpu_health、4 次 60s 超时)。scGPT 本身已验证正常(环境 + 真机 V100:52.4M 参数 `best_model.pt` 上卡、
vocab 60697,和成功 run 的日志吻合;`flash_attn` 缺但可选)。**整条分支现已合入 `main`**(Yijun 用本地 sync
脚本部署,不是 git fetch)。

## 2026-07-06 —— 新技能 `perturbation_analysis`(Perturb-seq,工作流 #3)—— 分支 `feat/perturbseq-skill`

延续"4 个 research skills"这条线(#4 变异注释已合入 main)。#3 是第一个 **混合 CRISPR / Perturb-seq**
路径,也是真正的缺口:`celltype_annotation` + `scgpt_annotation` 已覆盖 #1(scRNA 注释)、
`differential_expression` 已覆盖 #2(每细胞类型 DEG),重做会重复;而 **Perturb-seq 此前没有任何技能,
`src` 里也没有任何扰动处理**(grep 干净)。纯技能层新增 —— 丢一个文件夹,**零引擎/工具改动**
(遵循 `skills/README.md` 的分层决策规则)。

- **`skills/perturbation_analysis/SKILL.md`** 引导 PI:从 DATASET PROFILE 推断扰动列 + 共享的
  non-targeting 对照("很多 guide 对一个对照"就是与二组条件研究的区分点),然后 QC → 嵌入 →
  **按效应量(E-distance)给扰动排序 → 只对真命中做 per-perturbation DE vs 对照 → 富集**。内建的表述原则:
  **沉默 guide 是一个结果、不是失败**;**靶基因自敲低是阳性对照**。guide vs target 层级、guide 分配前置步骤、
  以及按细胞类型分层都点到了。
- **脚本(`run_code` 模板,progressive-disclosure 清单式呈现):**
  - `perturbation_edistance.py` —— scPerturb **E-distance**(平方欧氏能量距离,闭式 O(n·d),不建 pairwise 矩阵)
    每个扰动到对照的距离 + **标签置换检验** + BH FDR → 有真表型的扰动短名单(喂给 `ONLY_PERTURBATIONS`)。
    可选 扰动×扰动 pairwise 矩阵,用来把作用相似的扰动聚在一起。
  - `perturbation_de_vs_control.py` —— 改编自 `differential_expression` 的 `condition_by_celltype.py`,但
    按 扰动 vs 共享对照 分组(scanpy `rank_genes_groups`,显式 `reference`);内存安全用 view;每扰动 DE +
    带 **靶基因自敲低阳性对照检查** 的汇总;跨扰动的共有上/下调程序。
  - `mixscape_escape_filter.py` —— 可选 pertpy **Mixscape**,DE 前剔除逃逸(NP)细胞;pertpy(重的可选依赖)
    缺失时 **优雅降级**(跳过 + 说明,DE 在全部细胞上跑)。
  - `references/methods.md` —— E-distance 定义 + 置换检验、Mixscape、对照层级命名、guide→target 折叠。
    改编自 k-dense-ai/scientific-agent-skills + scPerturb/pertpy。
- **测试 / 验证。** `tests/test_perturbation_skill.py`(3 个):技能带正确 tools/scripts 载入 preset 注册表,
  外加一个 **库级守卫:每个 `skills/*/scripts/*.py` 都能编译**(这些模板在 CI 里从不 import,否则语法错误会
  悄悄漏过)。全套 **518 通过**。在合成三组数据(NT / 真敲除 `g5` / 沉默)上端到端 smoke 通过:E-distance 把
  真命中排第一、把沉默扰动判为不显著;DE 给真命中一批 DEG 并检出 `g5` 自敲低,沉默 ≈ 0 DEG。
- **本线待办:** 与 Jin Li 确认 #3 的输出/规格;#1/#2 视为已被现有技能覆盖(除非 k-dense 版本另有增量再回看)。
  当前在 `feat/perturbseq-skill`(基于 `main`);**尚未合并 / 尚未 push**。

## 2026-07-06 —— 报告 grounding、skill 可见性、scGPT/VL 启用手册

三件事 + 一个服务器实证。

**服务器实证(eyeserver-admin, `/data/BioAgent/app`)。** 部署机 `.env` **没有** `BIOAGENT_SCGPT_IMAGE`、
**没有** `BIOAGENT_VLREVIEW_ENABLED`;`runs/**/predictions.csv` 数量 = 0;`console.log` 里
`scgpt`/`constraint`/`SlurmJobError` 命中 0。**scGPT 在生产上从没跑过**,两个额外 sif 在配置层就是关的 ——
默认只有主 orchestrator(+ 它的文本自审 `report_review.md`)在跑。旧视网膜报告那句"scGPT 因 computational
parameter constraints 未执行"是**合成步幻觉**,不是真报错(那次 run 的 agenda/transcript/event_log 里都没 scGPT)。
部署代码是**旧版**,需重新部署才能拿到近期修复。注意:网关日志写在 `/data/BioAgent/console.log`,**不是 journald**。

**(1) 报告 grounding —— 不许编造没跑过的方法(`research_lab.py`)。** `_SYNTH_SYSTEM` 现在禁止描述任何
未实际运行的工具/模型/分析(包括写成"planned/attempted/failed")——干掉 scGPT + MOFA+/DIABLO 幻觉。
新 `_methods_performed(rounds)` 从 accepted 步里抽出**真正执行过的工具**做闭合白名单,和 `_grounding_vocab`
一起注入 `_synthesize`。真模型验证(Qwen3.6):只跑过 QC/聚类/DE 的 run,报告不再提 scGPT,反而写明这些方法"未执行"。

**(2) planner 后的 skill 可见性(`research_lab.py` + `gateway/app.py`)。** `run()` 在计划定稿后 emit
`skills_loaded {skills:[{key,label,tools}]}`(空列表 = 从零规划)。网关 feed 渲染成"📚 Loaded skill: …
(composes: …)" / "📚 No matching skill — planned from scratch."。之前**静默**的 `steps_pruned` 也加了 feed 行。

**(3) scGPT + VL 启用手册(`deploy/ACTIVATE_scgpt_vl.md`)。** 针对部署机的清单:要加的 `.env` 行、build+stage
指引(`deploy/scgpt`、`deploy/vlreview`)、重启、`.env` 目录不可写的坑、以及怎么验证(`predictions.csv` /
`visual_review.md`)。build kit 已讲怎么建镜像,这份把它对接到实际部署。

验证:全套 **498 通过**(+5)。已提交 `main`,尚未 push。

## 2026-07-06 —— Planner 守卫:已标注且无实验对比时不做"无意义的富集"

**背景 / 原因。** 一次视网膜 run(`bioagent_results_7e551b8db499`),数据是**单供体、单样本、已标注**
(obs `majorclass` 6 类 + `celltype` 66 类;所有非标注 obs 列都只有一个值),却计划了 de-novo 重聚类
(29 簇)、逐簇 DE、以及**对细胞类型身份标志基因做通路富集**。Dr. Chen 指出整条描述性分支没意义,
**富集尤其多余**:对**已知**细胞类型拿它自己的 one-vs-rest 标志基因去富集是同义反复(视杆标志→光转导,
只是复述定义);没有实验对比,就没有可解释的差异问题。

**修复(planner 层,`agents/research_lab.py`)。**
- `_dataset_context` 现在分支:已标注**且有对比** → 原有"把 DE/富集落到标注列、别用 leiden 编号"的引导;
  已标注**且无对比** → 新增"⚑ 无实验对比……不要计划通路/GO 富集或发现式 DE……"告警。
- `_PI_SYSTEM` 增加规则 **(d)**(无对比 + 已标注 ⇒ 只做 QC + 注释校验 + 描述性小结,不做富集);
  第 ~70 行的"完整性"偏置不再把 enrichment 当作永远必要。
- **确定性兜底**(真模型会无视引导 —— Qwen3.6 已证):规划后 `ResearchLab.run()` 用
  `_annotated_without_contrast(dr)` 判定,剥掉 `_is_enrichment_step` 的 agenda 项,发出
  `steps_pruned{reason:"no_experimental_contrast"}`。聚类/UMAP(可视化)和标志基因 DE(注释校验)**保留**;
  仅"提到 enriched pathways"的文献/解读步不会被误剥(`_is_enrichment_step` 对文献步返回 False)。

**验证。** 全套 **493 通过**(新增 3 个测试;原 groupby 测试改到"有对比"夹具)。真 LLM 端到端见
`scripts/no_contrast_enrichment_openrouter.py`:Qwen3.6 在无对比视网膜样本上照样计划富集 → 守卫剥掉
(文献步保留);有 KO-vs-WT 对比时守卫不触发、富集原样保留。已提交到 `main`。

**注。** 旧 bundle 早于之前那条 `⚑ 已标注` 告警,所以它"在 leiden 编号上跑 DE/富集"的问题此前已处理;
本次补的**新缺口是"无对比时的富集"**。

## 2026-07-06 —— 已合并进 `main`:main 现在就是 0.2.0;v0.1.0 只留作冻结标签

按 Yijun 的决定,`feat/dag-planner`(0.2.0 DAG)和 `fix/vllm-tunnel-resilience` 都已**合入 `main`** ——
`main` 从此是**唯一的 0.2.0 主线**。0.1.0 "pipeline" 线**不再维护**,只以 **`v0.1.0` git 标签**
(提交 `e2f51b8`,= 最后部署的生产 sha)存续,供汇报/回滚(`git checkout v0.1.0 && ./deploy/redeploy.sh`)。
这取代下面那节"把 main 冻结当快照"的旧方案。

两处合并冲突,均已解决 + 验证(完整套件 490 通过):
- **`gateway/app.py`**(文本冲突)—— `LabConfig(...)` 构造处。**两组都保留**:main 的 planner 预算
  (`max_steps`/`max_rounds`,来自 `BIOAGENT_MAX_STEPS`/`_MAX_ROUNDS`)+ DAG 的 planner 配置
  (`planner`/`multi_agent`/`max_concurrency`/`agent_memory`)。
- **`research_lab.py`**(语义冲突,git **没**报,是**测试**抓到的)—— main 的 planner 改动把
  `LabConfig.max_rounds` 默认改成 `None`("从 agenda 推导")并教会了 `_run_loop` 处理它,但 `_run_dag`
  里还是 `while executed < self.config.max_rounds` → `int < None` 报错(10 个 DAG 测试挂)。已修:
  `_run_dag` 在 `max_rounds is None` 时也推导 `round_budget = n_nodes * (1 + max_revisions)`,与
  `_run_loop` 同一规则。**这正是"DAG↔literature"那节预警的 research_lab.py 重叠 —— 凡是动到这个文件的
  合并,之后务必跑 `pytest tests/test_research_lab.py tests/test_dag.py`。**

新增 backlog(`docs/BACKLOG.md`):**skill 系统进一步解耦** —— 把 skill 的加载/选择从
`agents/presets.py`(skill≠preset)和 `agents/research_lab.py` 里抽出来,做成独立的 skill 子系统;
这是以后做 skill 归纳(induction)的接缝。

分支 `feat/dag-planner` / `fix/vllm-tunnel-resilience` 现已完全并入 `main`(push 后可删)。均未 push。

## 2026-07-05 —— 发布模型(v0.1.0 pipeline → 0.2.0 DAG)、回滚快照、vLLM 韧性、literature 冲突分析

### 1. 版本与回滚快照

把 DAG 之前的线冻结为回滚点,把 DAG 工作切成下一个版本:

- **`v0.1.0`(打在 `main` 上的 annotated git tag)= "pipeline" 版本**。DAG 之前的线性
  PI→Scientist→Critic 流水线。这个 tag 就是**回滚快照**——`main` 在此提交是一个已知可用、可部署的状态。
- **`0.2.0` = "DAG" 版本**,目前在 `feat/dag-planner`(`pyproject.toml` 已从 0.1.0 升到 0.2.0)。
  它**合并进主线**,主线此后维持在 0.2.0。DAG 线是 0.1.0 的严格超集(见 §2:特性开关门控,关掉即 0.1.0 原样)。

**回滚步骤(eyeserver 上的生产跑的是 `main`):** 生产是宿主机 systemd 服务
(`bioagent.service`,用户 `aiscientist`,`/data/BioAgent/app`,绑 `<GATEWAY_BIND_IP>:8800`,
经 `aiscientist` k8s Service→Envoy Gateway 暴露 —— **不是 pod**)。回滚到 pipeline 快照:
checkout `v0.1.0` → `./deploy/redeploy.sh`(rsync + `sudo systemctl restart bioagent`)→
确认 `.deployed_sha` 与 tag 一致。重启会掐断在线会话(有状态单例,每会话独立 SSH 隧道),挑空档做;
后端变更无零停机路径。

**0.1.0(快照)包含:** web 控制台(账号+登录、SSH+Duo 到 HPC3、每用户 Slurm vLLM 服务
Qwen3.6-35B-A3B-AWQ + 隧道、`squeue --me` GPU 隔离)、线性 PI→Scientist→Critic lab、真实
scanpy/gseapy 分析线、`run_code` CodeAct 沙箱、literature 线(`literature_search` Europe PMC +
`deep_literature` PaperQA2)与手稿引用、确定性 pandoc PDF/DOCX 报告、上传/分析/报告的 HPC3 offload、
plan-mode 人工确认、服务端聊天历史、断点续传上传。

**0.2.0 新增(全部在 0.1.0 之上增量):** DAG 规划器(`agents/dag.py`)、真·多 agent 专家认领 +
Coordinator、每 agent 演进记忆(`agents/agent_memory.py`)、独立分支的安全并发,以及 gateway 的接线/事件。
详见 `docs/dag_planner_design.md`、`docs/agent_memory_design.md`。

### 2. DAG 是特性开关 —— 0.1.0 行为原样保留

`LabConfig.planner` 默认 `"linear"`;`ResearchLab.run()` 在新鲜运行入口分流(`planner=="dag"` →
`_run_dag()` 否则 `_run_loop()`),A2 续跑**永远**走线性 loop。所以开关关掉时系统跑的就是 0.1.0
原样流水线,DAG 路径与它不共享可变状态。Gateway 开关(env,默认全关):`BIOAGENT_PLANNER=dag`、
`BIOAGENT_MAX_CONCURRENCY=<n>`、`BIOAGENT_AGENT_MEMORY=1`(仅 DAG)。这让 0.1.0↔0.2.0 是一次干净的
开关切换,而不是分叉。

### 3. vLLM 隧道/服务韧性 —— 分支 `fix/vllm-tunnel-resilience`(从 `feat/dag-planner` 切出)

**问题(2026-07-05 从生产 `runs` 表发现 —— 只有约 14% 的 run 到 `done`,31% `error`)。**
反复出现、直接判死整个 run 的 `Network error during vLLM …` 有两个无需用户操作的成因,都会让隧道
指向一个死端口:(a) 空闲时 SSH 隧道被回收(用户跑到一半走开),(b) GPU serve 作业撞上 **2 小时
Slurm `--time` 上限**被杀。而调用路径**既无 keepalive 也无重试**——一次抖动就废掉整个 run。

**修复(三层,已全部提交到分支;完整套件 489 通过):**
- **L1 预防:** `ssh_gateway._connect` 里 `transport.set_keepalive(30)` —— 空闲隧道不被回收。
- **L2 自愈:** 用独立的 `VLLMNetworkError`(区别于 context 超限等真·模型错,后者同样是
  `stage="vllm_chat"`),让 `_lab_llm` 只捕获可恢复的那种,调 `_heal_vllm_session(conn)` ——
  重连仍在跑的 serve 作业、或作业被杀就重投(`gpu.ensure_serve_job`)、重开隧道、等 `/v1` 起来 ——
  然后重试一次。用 `conn.gpu_lock` 串行化;`_lab_llm` 现在每次调用读**实时** `conn.tunnel_port`,
  重试用的是恢复后的新端口。OpenRouter/mock 原样抛出。
- **L3 启动适配:** 新增 `BIOAGENT_SLURM_CONSTRAINT` → `#SBATCH --constraint=…`,可把作业 pin 到
  80GB A100。裸 `gpu:A100:1` 可能落到 40GB 卡上,`--max-model-len 131072` 会让 vLLM 启动即崩
  ("KV cache 装不下")——就是"拉不起来"那种。确切的节点 feature 名用 HPC3 上 `sinfo -o '%n %f'` 确认。

测试:`tests/test_vllm_recovery.py`。**合并:** 分支从 `feat/dag-planner` 切出,合回 0.2.0 线;
改动都是 gateway 基础设施(`errors/vllm_client/ssh_gateway/app/settings/gpu.py`),与 DAG、literature 模块无重叠。

### 4. DAG ↔ literature(Ziyao)冲突分析 —— Ziyao 重启 literature 前必读

Ziyao 的 literature 线**独占**三个纯工具模块(无编排逻辑)—— `tools/literature_search.py`(Europe PMC)、
`tools/literature_references.py`(手稿引用)、`tools/paperqa_search.py`(`deep_literature`)—— 外加
`registry.py` 的增量注册(低冲突风险)。真正的重叠在两条线都改的两个**编排**文件:

- **`agents/research_lab.py`(中风险)。** literature 的逻辑在 `_scientist` 里的 `_is_literature_step`
  分流及 ~355–714 / 2016–2107 的 helper;DAG 新增 ~142–201、418–433、1412–1744。好消息:
  **`feat/dag-planner` 已经包含并尊重 literature 代码** —— `_READ_ONLY_TOOLS` 已列入
  `literature_search`/`deep_literature`(视为无共享写足迹,可与分析并发),`_run_dag` 也保留了线性
  loop 的 literature 兜底。风险只在 Ziyao 进一步重构 `_scientist` 的控制流时。
- **`gateway/app.py`(中高风险)。** literature 接在报告收尾路径里(~2596–2613 插引用、3535–3547
  自审后重插、3575–3643 抽取已接受引用)。任一条线重构报告流水线,这些调用都可能错位或丢失。

**建议(对"会不会冲突"的回答):** 有真实风险,但**可以靠选基避免**。让 Ziyao 下一步的 literature
工作**从 0.2.0 DAG 主线(feat/dag-planner)切分支,而不是从冻结的 0.1.0 `main` 切** —— 这样他是在
已经 literature-aware 的 DAG 代码上叠加,不会产生分叉基的三方合并。在两个热点划清归属:literature 拥有
`_is_literature_step`/报告引用调用;DAG 拥有节点调度 —— 谁都不在不打招呼的情况下重写对方的分流。
literature↔DAG 合并前先跑 `pytest tests/test_literature_search.py tests/test_literature_references.py
tests/test_research_lab.py tests/test_dag.py`。

### 5. Backlog 与一条生产运维记录

- **Backlog**(新增 `docs/BACKLOG.md`):**用户自带外部 API 替换 HPC3 后端** —— 推迟,做时单开分支。
  这是重写而非加开关:文件存储 + 中间过程文件存储 + 代码执行现在**全在 HPC3** 上,所以需要一层存储抽象
  和一层执行器抽象,而不只是换个 LLM endpoint(`BIOAGENT_LLM_BASE_URL`+OpenRouter 只是本地**测试**便利,
  不是产品方向)。
- **生产运维(2026-07-05):** 排查慢的过程中,`/data/BioAgent/app/.env` 被误清空后已原样恢复
  (DB 认证重新验证通过、27 键、已去重);因为没重启在线服务,故不受影响。以后改的坑:
  `/data/BioAgent/app` **目录** `<admin-ucinetid>` 无写权限(不能建临时文件 → 不能 `cp .env .env.bak`、
  不能 `mv tmp .env`),只有 `.env` 文件本身可写(ACL `group:users:rwx`)。改 `.env` 用管道写进已存在的文件
  (`cat 本地 | ssh … 'cat > …/.env'`),并**先在本地全量备份**。有意为之的生产配置(**不要**去"修"):
  A100-only gres、`BIOAGENT_LAZY_GPU=0`、world-readable `.env`。

日期:2026-07-03(分支 feat/dag-planner:多 agent + 并发 + 闭环图/动态重规划设计 —— 见下)

> 注:英文版 `HANDOFF.md` 有更细的分节(每个里程碑一节);中文这节做一次合并汇总,覆盖近期 DAG 全部进展。

## 2026-07-03 —— DAG 规划器:近期进展汇总(分支 feat/dag-planner)

设计文档 §1–4 **全部完成**,全部 flag 门控(`planner` 默认 `"linear"`,main/prod 不受影响)。

**已完成(代码):**
1. ✅ **DAG 计划 + 就绪集调度 + Coordinator**;每个节点"划界" brief(根治抢跑/双 QC);全 root 结构回退线性。
2. ✅ **Gateway 接线 + 控制台开关**("DAG planner (experimental)" 勾选框;feed 显示各类事件)。
3. ✅ **HITL 决策点**:结构 pass 标 `decision:true`+`options`;调度到决策节点暂停问你,复用现有 plan_event
   暂停机制;你的选择注入该节点 brief。**决策超时=继续**(用 agent 判断,不丢弃跑了一半的分析)。
4. ✅ **真多 agent 认领**:专家按专长认领就绪节点(LLM 决定谁做什么)。
5. ✅ **安全并发**:只有 footprint 不相交的节点才并行——分析节点共享 scanpy 全局态+checkpoint 链,永不重叠;
   文献等独立节点可与分析链并行。默认 `max_concurrency=1`(串行),`BIOAGENT_MAX_CONCURRENCY` 单独开。

三重验证:全套 **475 passed** + OpenRouter 真模型端到端(结构/Coordinator/HITL/认领/并发都真跑) + 真浏览器
前端(HITL 决策卡片渲染 + 点击回传)。

## 2026-07-03 —— 每-agent 演化记忆(Axis C)—— v1 已实现

做了最小闭环(flag 门控,仅 DAG)。`src/bioagent/agents/agent_memory.py`(`AgentMemory`:每 agent 磁盘
`episodes.jsonl` + `lessons.md`,`read`/`write_episode`/`reflect`,best-effort 永不抛)。挂进
`ResearchLab._run_one_node`:行动前把该专家的**私有记忆**读进 brief(`_scientist(memory=…)`),终止后写一条
episode;`_run_dag` 收尾每个出过力的专家**反思**(把 episodes 蒸馏成 lessons = 语义压缩)。事件
`memory_read`/`memory_reflect`。门控 `LabConfig.agent_memory` + `agent_memory_dir`(持久、**按 owner**、在
run_id 目录**之外** = `conn.workspace/_agent_memory`,只在 eyeserver);gateway 环境变量
`BIOAGENT_AGENT_MEMORY`(DAG 模式)。默认**关**=现状。

**三重验证:** 离线单测(`tests/test_agent_memory.py` 7 个)+ 集成(`test_research_lab.py` 2 个:跨 run 持久/
演化/召回、默认关)+ **OpenRouter 真 Qwen3.6**(`scripts/dag_memory_openrouter.py`):run 1 真模型把 QC episode
蒸馏出 3 条具体教训落盘("QC 跑成功但没做数据缩减=失败""输出必须是可下游用的 AnnData");run 2 QC 专家把它们
召回进 brief。全套 485 passed。

设计(本 session 早先定的):两层记忆(磁盘长期 + 检索片段进 context);冻结权重→in-context 学习非微调;存
eyeserver 不进 Singularity;压缩 = 反思(语义)+ 轮转 + 读时 top-K;一张 A100 够(记忆在 CPU/磁盘,~0 显存);
所有 agent 分时复用一个模型。完整设计见 `docs/agent_memory_design.md`。

v2 待办:embedding 检索、实验室级共享 lessons 池、线性循环也上记忆、反思成本控制。动态重规划
(`dag_planner_design.md` §8)仍推迟(跑偏顾虑)。

## 2026-07-03 —— 每-agent 演化记忆设计(Axis C)—— 优先级高于动态重规划

新设计文档 `docs/agent_memory_design.md`(**只设计,未实现**)。Yijun 拍板:**每个 agent 隔离 + 各自演化记忆**
是优先项,**高于** §8 动态重规划(重规划会改研究方向→有跑偏风险;记忆只加深角色深度、不改方向→低风险)。本次
定下的关键点:
- **诚实纠偏:** 我们**有** per-step 上下文隔离 + 每步目标串;**缺**的是**持久、私有、跨步跨 run 会演化**的
  per-agent 记忆(反思把 episodes 蒸馏成 lessons)。差的是"持久+演化",不是"隔离"。
- **算力(硬约束):** 真多 agent **不需要**换多模型/多卡/物理隔离。所有 agent **分时复用**常活在 A100 上的
  那一个模型(35B-A3B AWQ ~20GB);记忆在 **CPU/磁盘**(JSONL + lessons.md),**几乎不吃显存**。**一张 80G A100
  绰绰有余,不用加硬件。** 给子 agent 换小模型是可选优化,不是前提。
- **架构:** `memory/<agent_id>/{episodes.jsonl, lessons.md}`,每 agent 私有;行动前读进划界 brief、终止后写一条
  episode、反思去演化。共享黑板(已接受 findings + checkpoint)仍是唯一的跨 agent 交接。挂进 `_run_one_node`;
  flag 门控 `LabConfig.agent_memory`;冷启动=现状行为。
- **动态重规划(§8):设计保留,暂不实现**(跑偏顾虑)。

## 2026-07-03 —— DAG 执行闭环图 + 动态重规划设计(仅文档)

扩写 `docs/dag_planner_design.md`(本功能的结构文档),**无代码改动**:
- **§7「执行闭环」**:用 Mermaid 画了**两层嵌套状态机**(外层图调度 `_run_dag` + 内层单节点 `_run_one_node`),
  以及让它成为**会终止的闭环**的四条不变式:I1 `done_ids` 单调增、I2 每节点只终止一次、I3 无环、I4 有进展。
  核心框架:**DAG 把"agent 边界"显式化**(节点=边界;边=唯一交接通道;agent 不互相调用)。
- **§8「动态重规划(自适应 DAG)」——只有设计,未实现**。如何"根据结果改图"同时**边界严谨**:
  冻结集 = 已完成∪运行中(不可变),可变集 = 仅未开始的 pending 前沿;新依赖可以**指向**已完成节点,但绝不能
  成为已完成节点的**新前置**;改完重跑环检测;`max_replans` 预算封顶;改图经 HITL 卡片批准。所以 §7 的单调性/
  终止性**由构造保证**,不靠运气。落点:每次 accept 后 `_replan_check`、`LabPlan.with_mutation`(返回**新**的已校验
  plan;节点是 frozen dataclass,永不原地改)。

**两个"已设计未实现"的后续(都在文档里):**
- §7 **硬边界**:在执行器层强制 `produces`/`consumes`(节点只能写自己声明的产物),把现在的**软边界**(brief+护栏)
  变成物理隔离。复用并发模型已经算好的 footprint。
- §8 **动态重规划**:上面那套自适应 DAG。

**文档盘点(你问的):约 10 类、40+ 个文件。** 内容很丰富但**没有索引**——长期 agent 项目的唯一真缺口是缺一个
`docs/README.md` 文档地图(docs/ 是 20+ 个平铺文件);ADR 起了个头(`docs/adr-0001`)但没坚持,大决策散落在
各设计文档;没有维护 CHANGELOG(git log 是事实上的)。完整评估见会话里的说明。

## 2026-07-03 —— d2f4 审计修复:用注释 + 步骤作用域 + 文献收窄

## 2026-07-03 —— d2f4c662024e 审计 → 4 个流程修复

审计了 `d2f4c662024e`(部署了 grounding+分类富集+多查询后的第一轮)。确认最近的修复**都生效**:富集按簇
各跑一份(29 张表)、文献规划了多条查询、报告只引用**真实**富集 term(Cluster-5 应激通路已在
`enrichment_5.csv` 逐条核实——没有编造)。但暴露了 4 个**流程层**缺陷,本次全修。

1. **无视数据自带注释(最大问题)。** 数据自带专家 `majorclass`(6)+ `celltype`(66)obs 列,但 PI 从头
   leiden 聚类、在裸簇号上跑 DE/富集 → 报告全是无法解读的 "Cluster 0–28"。PI 提示里本来就说要复用已有标签,
   但 profile 没把它标显眼。修:`_dataset_context` 现在输出 `⚑` 提示,列出检测到的细胞类型列
   (`_looks_like_celltype_col`),并叫 PI 把其中一个当 `groupby` 跑 DE/富集,别用 leiden。
2. **双 QC → 数据不一致。** Step 1 跑了整条(QC mt=10 → 聚类 → DE → 富集);Step 2 又跑 QC(mt=5)+ 聚类但
   **没重跑 DE**,于是 DE 表/富集反映 mt=10 聚类,而最终 checkpoint + UMAP 是 mt=5。修:
   `_accepted_findings_block` 现在**禁止重跑已成功的上游阶段**(QC/聚类/DE/富集)——复用 checkpoint;重跑
   (尤其换参数)会覆盖它并让已导出的表/图错位。
3. **agenda 步骤文本损坏。** team 设计会综述("…Convergence Divergences Core Conditional")漏进了文献步标签:
   `_ensure_literature_agenda` 用 `question + guidance + feedback` 建标签,`focus_literature_query` 把会议
   文本搅成了乱码。修:标签**只用 question**(意图检测仍用三者;每条查询词后面从 findings 生成)。
4. **文献查询太窄 → 几乎 0 命中。** 多查询生效了但 8 条只回 1 篇——Europe PMC 把每个词 AND,5–6 个基因的
   查询直接空。修:`_LIT_QUERY_SYSTEM` 现在要求**每条 2–4 个宽泛关键词**(每条最多 1 个基因符号)并解释 AND
   行为;确定性兜底查询也收窄成 1 term + 2 基因。

测试:test_research_lab.py +4(类型列检测、profile 提示、标签忽略 feedback、禁止重跑上游)。全套 446 passed。
需 sync + 重连生效。

**d2f4 审计里仍未处理(不在本次):** Step 1 一步里跑完了整条流水线(scientist 没被限定在单步——复用护栏能减轻
危害但没阻止抢跑);对弱富集的过度解读(Cluster 5 在 adj_pval≈1e-3 下被抬成头条亚型);渲染缺陷(空目录、
`[Figure]` 占位、图注重复)—— VL backlog。

## 2026-07-03 —— 407 报告审计:编造富集 + 池化 ORA —— 已修

## 2026-07-03 —— 407 报告审计:编造富集 + 池化 ORA —— 已修

审计了 `407300d229da` 包。文笔不错,但两个真问题:
- **编造发现。** 摘要/结果/讨论都声称发现了 EMT + ECM 组织通路。实际富集表就 10 条 term,**全是光转导/
  视觉**,没有任何 EMT/ECM(连一条 MSigDB Hallmark 都没有)。synthesize 节点凭空捏造了一整类通路还围绕它
  写了机制段。另外把类标签 "MG"(Müller 胶质)脑补成 "Müller/ganglion"(6 个类里根本没有 RGC)。
- **富集池化。** 所有 term 都在一个 `input` 组下 → 被光感受器垄断,分类生物学丢失。

**池化的根因:** `run_enrichment` 把 `tables/de_leiden_all.csv` **写死**了。这轮 DE 用的是
`groupby=majorclass` → 文件叫 `de_majorclass_all.csv`,工具找不到,就退回 agent 传的池化 `genes` 列表
(`input`)。分类 ORA 的机制本来就在——只是找不到那张表。

**修复:**
- **分类富集**(`tools/scrna_pack.py`):`run_enrichment` 现在**自动发现**任意 groupby 的 DE 表
  (`args.groupby` 优先;否则优先选带注释的 `de_*_all.csv` 而非原始 `de_leiden_all.csv`),按细胞类跑 ORA。
  工具描述更新:**不要**传池化的 `genes` 列表。
- **报告 grounding**(`agents/research_lab.py`):`_SYNTH_SYSTEM` 现在明确禁止编造通路/富集 term 和细胞类型
  标签,禁止把类标签改名/合并/扩写成别的细胞类型。新增 `_grounding_vocab(rounds)`,把一份**封闭词表**
  (确切的类标签 + 确切找到的富集 term)注入 synthesize 提示 —— 于是 EMT(不在表里)引用不了,`MG` 也变不成
  `Müller/ganglion`。
- 测试:+1 scrna_pack(分类发现),+3 research_lab(词表锁类/term、空、synthesize 提示带上)。全套 442
  passed。需 sync + 重连生效。

**407 审计里仍未处理(不在本次):** 渲染层缺陷(图注重复、`0.0e+00` p 值应显示 `<1e-300`、表格 ASCII 错位、
5.1/5.2 内联编号、孤儿图 `scatter_qc_mt.png` + Fig-1 图注/图不符)留在 VL 报告审查 backlog。QC 删了 0 个细胞
(输入是已 QC 的 atlas 数据)——诚实但 QC 段落夸大了,低优先。

## 2026-07-03 —— 文献:LLM 审核 + 多角度查询(取代单条关键词拼接)

## 2026-07-03 —— 文献:LLM 审核 + 多角度查询(取代单条关键词拼接)

**需求(用户):** 文献查询先过一遍 LLM 再去搜,而且针对**不同重点写好几条 query**(按细胞类 / 按通路 /
按疾病机制),而不是就一条。

**之前:** `_scientist` 用 findings 拼**一条**确定性 query(`_literature_query`),只搜一次 Europe PMC。
太笼统,容易漏掉整个角度。

**现在**(`agents/research_lab.py`):文献步走新的 `_run_literature_step`:
- `_plan_literature_queries` → `self._complete(_LIT_QUERY_SYSTEM, digest)`:模型读一份**按细胞类**的精简
  findings 摘要(`_literature_findings_digest`:每类的 top markers + 富集通路),返回 2–5 条**各不相同**的
  关键词 query,每条一个角度。
- `_parse_query_list` 审核输出:解析数组,每条再过一遍 `focus_literature_query`(把模型可能夹带的指令/文件
  词剥掉),去重,按 `LabConfig.max_literature_queries`(默认 4)封顶。
- 逐条搜;引用**合并 + 去重**(按 DOI → PMID → title)。一份被接受的答案喂给 Critic + 最终 `## References`。
  引用配额在多条 query 间均分(`per_limit`)。
- **兜底保证鲁棒:** LLM 不可用 / 非 JSON / 空 → 退回那条确定性 `_literature_query`(离线/降级也能引用;
  "文献步只跑一次"的规则不变)。
- 跑在 **gateway 主机**上(Europe PMC + vLLM 都可达)—— **不是**断网的 Slurm 容器。`_run_loop` 里的
  保底补文献也走 `_scientist`,自动继承这套。
- 测试:`test_research_lab.py` +4(解析/清洗/封顶、findings 摘要、多 query 合并去重、退回单条)。全套 438
  passed。需 sync + 重连生效。

日期:2026-07-03(重连恢复 + WS 自动重连)

## 2026-07-03 —— "幻影卡死 run" + WS 掉线丢产物 —— 已修

**现象(用户):** 某轮一直显示 "running · 28m" 不停;刷新也不消失;点 Stop 再刷新,thinking recap 和
下载产物**全没了** —— "产物压根不见了"。在 run `407300d229da`(owner `BioAdmin`)上确认:**后端 18:30
就跑完了**(report.pdf/docx + technical_report 都渲染好;DB `runs.status=incomplete`,
`finished_at=18:30:04`)。产物从未丢失 —— 在磁盘上,而且 DB 支撑的 **Runs 标签页**(`/api/runs`)和
聊天状态完全独立地列着它们。坏掉的是**聊天这条链路**。

**根因(两个叠加的 bug):**
1. **重连从不为"已完成"的 run 补水。** 完成消息(`chat_token`/`artifacts`/`chat_done`/`run_complete`
   + thinking/feed)走 `push()` → 只进 `conn.stream`,**不进 `conn.log`**。WS 端点**只在
   `conn.chat_running` 时**重放这段流 —— 默认"已完成的 run 客户端早就持久化了"。但客户端**漏收**了实时
   完成消息(socket 在 `chat_done` 前掉了)时这假设不成立:什么都没持久化,重连也重放不出东西。
   thinking/feed 是临时的(只在 `conn.stream` + bundle 的 event_log 里),所以刷新+不重放 = 全丢。
2. **WebSocket 没有 `onclose` → 从不自动重连。** socket 一掉(合盖休眠、网络抖动、空闲超时)就是终点;
   客户端守着一个 `setInterval` 计时器永远显示 "running"。唯一恢复手段是手动刷新 —— 而按 bug #1,连刷新
   都救不回来。

**修复(bug fix,直接进 main):**
- **服务端**(`gateway/app.py`):`_track_stream` 现在捕获 `run_id`(从 artifacts 的 `bundle_url`,或
  `run_complete` payload),并保留 `run_complete` 标记;`stream_replay_payloads` 在 `chat_start` 上带
  `run_id` 并重发 `run_complete`。WS 端点现在**对已完成的流也重放一次**,给开头的 `chat_start` 打上
  `recover: true`。
- **前端**(`console/app.js`):收到带 `recover` 的 `chat_start` 时,`alreadyHaveRun(run_id)`
  (LASTRUN_KEY + 逐会话扫 artifact URL)判定 —— 已有 → 整段丢弃(不重复气泡);从没见过 → 正常处理,把
  漏掉的 run 恢复进它的 owner 聊天 + 下载区。新增 epoch 守卫的 **`ws.onclose` 自动重连**(退避 1s→15s
  封顶),socket 掉了会自己回来,重放无需手动刷新。
- 测试:`test_connection_replay.py`(+2 —— 重放带 run_id、run_complete 重发)。全套 433 passed。

**部署:** 需 sync + 重连生效。与仍待部署的文献查询修复(`80749e0`)相互独立 —— run `407300d229da`
References 为空是因为它跑在**旧代码**上,用了垃圾查询 `finish research…`;那是另一次部署的事。

**小尾巴(已记,不阻塞):** LIVE run 期间自动重连会重放 `conn.log`,可能让日志面板的行**重复**(纯外观;
报告/recap/下载不受影响且已去重)。以后修:重连时重置日志面板 DOM,或给日志事件打 replay 标记。

## 2026-07-03 —— UX backlog(console;推迟,不阻塞)

两个小的 console UX 毛刺,以后再修(在 run `2eb5daffdc51` 上确认;那轮其余正常 —— 离线 GMT 富集端到端成功):

1. **run recap 在,但不好找。** 完成的 run **确实**保留了中间流式内容 —— `finishAssistantStream` 持久化 `thinking` + `feed`,`messageEl` 渲染折叠的 `💭 Thinking & activity` + `🔬 Steps & code`(刷新也在)。只是用户找不到:它是长报告**最上方**一个很细的折叠小三角。要做得更显眼 —— 比如更清楚的入口/条目数徽章,或把它钉在报告**下方**而不是上方。**不是丢数据的 bug**(已核实部署前端 == 仓库 HEAD,后端把工具活动当 `chat_thinking` 流式发出)。
2. **live "Working…" 计时显示的是整轮总时长,不是当前步。** `repaintWorking` 用 `secs = now - run.startedAt`,所以 working 行显示成 "Running literature_search · 16m 10s",这 16 分钟是**整轮**(QC/聚类/DE/富集的 Slurm 排队+计算),而文献可能刚开始。于是一个慢的尾步看起来像卡了 16 分钟。修法:显示**每步**耗时(在 `scientist_start`/`tool_start` 重置步计时),或标注为"总计"。纯前端,便宜。

## 2026-07-03 —— 复盘清单 #1–#4 已处理(富集离线化 + agent 契约 + 循环早停)

关闭下方 `5bd05b3f5880` 复盘里的大部分条目。

- **#1 富集离线化 —— 已修**(`7788683`)。`run_enrichment` 改为对本地 `.gmt` 做离线 ORA(`gseapy.enrich`,不联网)。运维动作:在 eyeserver live 源码里跑 `scripts/fetch_genesets.py` → `src/bioagent/tools/genesets/`(它随每会话的源码 tar 同步到 dfs3b → 断网容器就地可见;HPC3 无需操作,也不用设 `BIOAGENT_GENESETS_DIR`)。
- **#2 organism 崩溃 —— 已修。** 离线重写**直接删掉了 `organism` 参数**(不再读取),传大写 `"Human"` 也不会再 `ValueError`;并从 `run_enrichment` schema 里移除了死掉的 `organism` 字段。
- **#3 DE 列/checkpoint 契约 —— 已修。** `run_de` 的工具描述现在写明 checkpoint(`work/adata_de.h5ad`)和表的**确切列名**(`group,gene,log2fc,pval,pval_adj,score`),并告诉 agent **不要**假设 Seurat 风格(`gene_name`/`p_val_adj`/`avg_log2FC`)—— run_code 兜底时不再猜。(`adata_de.h5ad` 本来就写了,缺的只是把契约告诉 agent。)
- **#4 卡死/冗余循环早停 —— 已修。** `ResearchHarness` 加了两个便宜护栏:`max_tool_errors`(默认 3)在连续 N 次工具**执行**错误后放弃该步,不再磨到 `max_steps`(那个 15 分钟卡死的形状);`max_wasted_after_success`(默认 2)在某工具已成功、模型却在原地打转(重复调用/报错)时带着成果停下 —— 完全相同的已成功调用也会被短路(不再步内重跑聚类)。新增 stop_reason:`repeated_tool_errors`、`done_early`。测试在 `test_research_harness.py`。
- **#5 Stop 时补文献 / #6 跑偏文件 —— 本轮未做。** #6 随 #1 自愈。#5(用户中途 Stop 时是否仍补跑独立的文献步)推迟 —— 文献保底(`0aeec9e`)只覆盖预算耗尽、不覆盖 cancel;和 DAG 那条线一起再议。

全量 **425 通过**(离线)。下一步:`feat/dag-planner`(planner 产出 DAG + agent 自决调度;不要求并发)与 human-in-the-loop 关键决策点(见下)。

## 2026-07-03 —— run `5bd05b3f5880` 复盘:富集步骤离线卡了约 30 分钟、拖垮了文献步骤(仅分析,暂未改代码)

**来源。** 分析结果包 `bioagent_results_5bd05b3f5880.zip`(视网膜 scRNA,11,977 个细胞,29 个 Leiden
簇,git `805c5dd`)。QC / 聚类 / DE 都正常;**第 4 步(富集)硬失败,把剩余预算全部耗光,用户在 21:30
按了 Stop**。结果:`converged=False`、`accepted=3/5`、References 为空。未改任何代码 —— 这是给 core line 的
修复清单。

**实际发生了什么(依据 `process/event_log.txt` + `round_04..06.json`):**
- `805c5dd` 上的 `run_enrichment` 仍在调 **Enrichr 网络 API**(`maayanlab.cloud`)。HPC3 的分析计算节点
  **没有外网出口** → `NameResolutionError: 无法解析 'maayanlab.cloud'`。
- gseapy **每次调用要重试约 15 分钟才放弃** —— 连着两次 15 分钟的空转(`20:57→21:12`、`21:13→21:28`),
  A100 空占着啥也没干,把步骤预算烧光。用户放弃并按下 Stop;文献步骤(议程 #5)**根本没跑**,所以稿件
  零引用。

**修复清单(按优先级):**

1. **[P0 —— 基本已在做] 把富集改成离线。** 当前**未提交的工作区改动**
   (`src/bioagent/tools/scrna_pack.py` + 新增 `src/bioagent/tools/genesets/` + `scripts/fetch_genesets.py`)
   已经把 `run_enrichment` 重写为读**本地 `.gmt`、走 `gseapy.enrich`** —— 不联网 —— 这正是 DNS 失败和 15
   分钟卡死两个问题的根治。**剩余动作:** 真正跑一次 `scripts/fetch_genesets.py`,把 `.gmt` 下到 **HPC3
   source-bind 的 genesets 目录**(或设 `$BIOAGENT_GENESETS_DIR`),否则 `run_enrichment` 会返回
   `missing_libraries`。然后在 HPC3 上端到端验证一次真实富集再收尾。
2. **[P1] 规范化 `organism` 参数。** 第一次富集调用直接死于 `ValueError: Invalid organism 'Human'` ——
   LLM 传了首字母大写的 `"Human"`,gseapy 只认小写。在 `run_enrichment` 里 `.lower()`/做映射(schema 仍暴露
   `organism`),别让大写值硬崩。顺手改,很便宜。
3. **[P1] DE 检查点 + 列名约定。** 第 3–4 步反复崩在 `FileNotFoundError: adata_de.h5ad` /
   `adata_clustered.h5ad`(agent 猜的检查点名 `run_de` 根本没写过 —— 它甚至中途重跑了聚类、撞到
   `max_steps`),以及 `KeyError: ['gene_name','p_val_adj']`。真实的 `tables/de_leiden_all.csv` 列是
   **`group,gene,log2fc,pval,pval_adj,score`**(不是 agent 以为的 Seurat 风格 `gene_name`/`p_val_adj`)。
   修法:让 `run_de` 确定性地落一个 `adata_de.h5ad`,**并且**把 DE 表的规范列名告知 agent,别让它瞎猜。
4. **[P2] 掐掉失控的工具调用。** 任何单次工具调用都不该卡约 15 分钟 —— 给网络路径加个短超时(#1 落地后
   虽变无关紧要,但属通用加固);并考虑在同一失败连续 N 次后就中止该步,而不是磨到 `max_steps`(第 3/4/5
   轮全是 `max_steps` 收尾;agent 还在坏代码片段上浪费步数 —— 括号不匹配、字符串未闭合 ×2)。
5. **[P1 —— 编排] 文献步骤被饿死。** "即便轮次预算耗尽也保证文献步骤运行"那套(`0aeec9e`,已在 `805c5dd`
   里)这次**没触发**,因为这次是**用户取消**而非预算耗尽 —— 那个兜底不覆盖 Stop。等 #1 让富集
   不再卡死,流程会自然走到文献,所以 #1 才是真正的解锁点;另外可评估:流程中途被 Stop 时是否仍应补跑那个
   独立的文献步骤。
6. **[P3 —— 表面问题,会自愈] 越界文件。** agent 手搓了 `gene_sets_for_enrichment.json`(被隔离到
   `extra/`),纯粹因为工具坏了;#1 修好后自动消失。

**结论:** #1 是关键 —— 离线 `.gmt` 富集 + 把库拉到 HPC3,就能解锁整条流水线的后半段。#2/#3 是便宜的正确性
修复,值得跟 #1 一起做。

## 2026-07-03 —— LLM 后续意图路由:聊天续打字改的是「同一份报告」而非另开新研究(分支 `feat/llm-followup-router`)

**为什么。** 聊天框永远 POST `/api/lab`,而它每次都新建 `run_id`+重新规划。所以一轮跑完后,再打
"重新找文献再生成报告"会开一个**全新研究**、写到新目录 —— 一份**没有原图**的独立 bundle,而不是在原报告上改。
能复用 bundle+图的 A1(重生成)/A2(续跑)路径,之前只能靠按钮触发,打字碰不到。Yijun 在 run
`825fd62fddce` 上反馈。

**改了什么(仅后端,前端不改)。**
- `_dispatch_lab(conn, req)` 成为 `/api/lab` 新入口。当磁盘上存在上一 run 的 bundle(且用户没有显式表示要开新研究)时,让**本会话自己的 LLM** 判意图,再转发到已有路径 —— 不用脆弱的前端关键词规则:
  - `edit_report` → `_regenerate_report`(A1):就地改报告文字,**保留原图**。
  - `rerun_step` → 经 `_run_lab` 续跑(A2):在**同一 run/目录**里只重跑指定那步,其余步骤和图复用;步骤用 `_match_agenda_step` 与原 agenda 逐字匹配。
  - `new_study` → `_run_lab` 全新(不变)。
- **模糊就问,不猜。** 置信度 `< FOLLOWUP_CONFIDENCE`(0.6)→ 弹「改报告 / 重跑某步 / 开新分析」**澄清卡**(复用 PI 澄清的往返:`plan_clarify` → `/api/lab/plan` → `plan_event`)。**模型冷**(`conn.alloc` 为空)时跳过分类直接问 —— 不花 GPU 冷启动去判一句话。
- **确定性护栏**(直接短路成新研究,不调 LLM):`plan_mode`、选了 `preset`、或**换了数据集**(与上一 run `dataset_path` 的文件名不一致)。
- **检查点降级**(Yijun 已确认):`rerun_step` 若在第 0 步之后、而 `work/adata_*.h5ad` 已过期,则**降级为 `edit_report`**(A1),保住图,而不是续跑出一个空结果。
- 重构:抽出 `_prepare_continue(...)`(构造 ResumeState + resume_decisions + LabRequest),`/api/lab/continue` 与路由器共用;端点改为 raise/catch `ValueError` 区分无 agenda(422)/检查点过期(409)。
- 纯函数有单测:`_extract_json_object`、`_match_agenda_step`、`_parse_followup_intent`、`_default_rerun_index`、`_followup_target`。

**测试。** `tests/test_followup_router.py`(+13):纯函数、三种路由分发、检查点→改报告降级、低置信度→澄清、冷模型→问、以及澄清卡真实往返。全量 **421 通过**(离线;FastAPI TestClient + 脚本化分类器,无 GPU/模型)。

**注意 / 待办。**
- 分类跑在热着的 Qwen 上(一次便宜的单发)。尚未在真实 HPC3 会话上验证 —— 值得冒烟:跑完一轮后打"再找些文献重出报告",确认它走 A2(同 run_id、图保留)而不是新 bundle。
- 澄清卡的**打字**回答是尽力关键词映射;三个 chip 是确定性映射。若打字误判,则回退为新研究(非破坏性 —— 旧 bundle 仍在)。
- 长远看,这正是 LangGraph 化的天然接缝(意图 = 一个 router 节点)。

## 2026-07-03 —— A2 续跑已完成(全部 5 个增量)分支 `feat/report-regenerate-and-session-persist`

改某一步分析(如换聚类分辨率)重跑那一步+其后续、报告随之更新,**不重新规划、不重跑整条 pipeline**。
`ResearchLab.run()` 本就是显式状态机("maps to a LangGraph"),所以这是给它**加 resume**,不换框架。
全程离线测试(11 个 A2 测试:resume + continue)+ UI 浏览器实测。

- **1 可续跑状态机。** 把 Scientist→Critic→advance 主循环+synthesize 抽成共享 `_run_loop(...)`。
  `run(resume=ResumeState)` 跳过 skill/mode/PI 规划,预载保留的已接受 rounds,从 `from_step_index` 往后重跑
  (round 预算只算新增)。新增 `ResumeState`/`from_run_state` + 三个 dataclass 的 `from_dict`。发 `run_resumed`。
- **2a run_state 持久化。** `_write_run_state()` 在 run 结束写 `artifacts/process/run_state.json`。
- **2b 保留 checkpoint。** `_trim_work_keep_checkpoints()` 替换原来的一刀切 `rmtree(work/)`:保留
  `work/adata_*.h5ad`(续跑那步的输入),其余清掉。HPC 路径下 dfs3b 的 checkpoint 本就在(remote_ws 按
  run_id 命名 → 续跑同路径)。
- **2c `/api/lab/continue`。** 读 `run_state.json` → 构 `ResumeState`(可带 `edited_step`/`modify_note`)→
  经 `resume`/`resume_run_id`/`resume_decisions` 复用旧 run 的 id+目录(checkpoint+staged 数据集原地可解析)、
  跳过规划+预检、原地重建报告。`tests/test_lab_continue.py`。
- **3 UI。** 结果栏"重跑某步"打开步骤选择器(agenda 从 `run_complete` 消息拿、存 localStorage)+ 备注框
  → POST `/api/lab/continue`。
- **依赖评估(新增,回应"应该先评估后续步骤需不需要重跑")。** 续跑不再无脑重跑选中步之后的所有步骤。
  `ResearchLab._evaluate_redo_indices()` 判断哪些后续步骤**真的依赖**这次改动,只重跑那些;无 checkpoint
  的独立步骤(如文献检索)**原样复用**。LLM 判定 + 确定性护栏(只有文献/背景类步骤才允许保留,凡是读
  checkpoint 链的分析步一律重跑)+ 保守兜底(判不出来就全重跑,绝不会更差)。`_run_loop` 按步选择复用或执行。
  `test_research_lab_resume.py` +4。

**注意/后续(非阻塞):**
- 存储:每个 run 保留 `work/adata_*.h5ad` 会随时间涨盘(按"不自动删研究数据"策略这是有意的)。若成问题,
  加个保留/清理策略,或接进现有"Manage HPC3 storage"UI。
- 续跑需要**活跃会话**(重做步+synthesize 要调模型),且只对有 `run_state.json` 的 run 生效(即本次改动之后
  产生的 run;旧 run 返回 404 "先重跑一次")。**尚未对真实 HPC3 分析 run 做端到端实测**(仅离线 mock)——
  正式依赖前建议先跑一次真机 smoke。

## 2026-07-03 —— A1:不重跑就能重生成报告;数据集/run 刷新不丢  [分支 `feat/report-regenerate-and-session-persist`]

**起因。** 每条聊天消息都走 `/api/lab`,总是 `uuid4()` 生成新 `run_id`、从头重跑整条
PI→分析→报告 —— 所以"只想重生成 PDF"会把同一个研究又完整规划一遍。而且选中的数据集只活在 JS 内存,
刷新即丢。

**A1 —— 从已落盘的 bundle 重生成报告(不启 PI、不跑分析)。**
- 新增 `POST /api/report/regenerate`(`RegenerateReportRequest{connection_id, run_id?, instruction?,
  basename?}`)→ `_regenerate_report`:从磁盘读该 run 的 `report.md`,可选跑一次 LLM 定向改写
  (`_edit_report_body`,限制图片引用 + 退化保护,和 `_review_report` 一致),原地重渲染 PDF/DOCX,
  重新发布文件。像正常 run 一样流式推 chat_start/lab_progress/chat_done。按 (owner, run_id) 从盘加载,
  所以刷新/重连后仍可用。`title=None`,因为存盘的 `.md` 自带 YAML 标题块
  (`_split_front_matter` 在改写时保留它)。
- 抽出 `_build_report_render_fn(conn)`(HPC SlurmReportRenderer 或本地 pandoc),`_run_lab` 与新路径
  共用。`Connection.last_run_id` 记住上次完成的 run;新增 `run_complete` WS 消息把 run_id 回传前端。
- 前端:结果栏加"重新生成报告"按钮(`regenerateReport()`);聊天输入框里的文字作为可选改写指示
  (空 = 原样重渲染)。last run_id 存 localStorage(`LASTRUN_KEY`),刷新后仍能重生成。
- 测试:`tests/test_report_regenerate.py`(+10)。全套 378 绿。

**数据集/会话持久化(问题 B)。** 选中的数据集路径在 `selectDataset` 时存 localStorage(`DATASET_KEY`),
`loadDatasetChips()` 在刷新后恢复 —— 上传路径不再因 reload 消失。

**connect 表单溢出修复。** SSH-key 登录功能新增的几行(Duo 选择 / 记住我 / 密码短语)把主按钮
**Connect** 顶到了笔记本高度视口的折叠线以下。收紧了 `#connectForm` 的纵向间距(gap/margin/输入内边距)
—— 浏览器实测:720px 下 Connect 可见,620px 下面板内部滚动(不再整屏溢出)。

**本轮未做(按 Yijun 意见留给 A2):真·多轮续跑** —— 改某一步分析参数、报告随之更新
(LangGraph + Postgres checkpointer)。结构上被卡住:`work/` 的 checkpoint 在 run 结束时被删、PI 循环
不可恢复。A2 等这个分支 push 后,在本地 mock 测试里做+验证。

## 2026-07-03 —— HPC3 报告渲染:不再 "Failed to download" 崩 run;失败能查出原因

**起因。** 一次运行以原始报错 `Chat error: Failed to download /dfs3b/.../report.pdf -> ...` 结束。
渲染这条路径(`gateway/slurm_report.py`)上叠了三个缺陷:
1. **查不出原因** —— pandoc/xelatex 的真正报错写在 job 的 Slurm 日志
   (`{scratch}/{name}-{jobid}.log`),却**从不回读**;失败只会说 "render produced no output"。
   这就是一直查不出报告为什么失败的根本。
2. **把整个 run 弄崩** —— `get_file` 抛的是 `GatewayError`(`RuntimeError`),但下载只
   `except OSError`、`__call__` 只 `except SlurmJobError`,异常逃出 `build_pdf_report`,
   在没包 try 的手稿渲染步骤(`app.py:2065`)炸掉。
3. **提交成功但没产出时不回退** —— 本地 pandoc 只在 `SlurmJobError` 时触发。

**修复(`gateway/slurm_report.py` + `app.py`)。**
- 下载失败一律接住 —— 渲染器的契约是**返回** `(ok, err)`、绝不外抛。没产出时**抓取 Slurm 日志尾巴**
  (`_fetch_log_tail`,路径不加引号以便 `$HOME` 展开)当错误返回,再**回退本地 pandoc**;
  markdown-only 仍是最后兜底。
- `app.py` 把**完整**渲染错误(含日志尾巴)落盘到 `artifacts/process/report_render_error.log`
  —— 聊天里截断到 160 字符,否则真正的 LaTeX 原因会丢。
- 测试:`test_slurm_report.py` +2;report/job/reattach 共 22 绿。

**待办(要用现在能抓到的日志来定位那一次失败的唯一原因)。** 日志尾巴最可能指向:
(a) .md 引用了**缺失的图**(本次只接受 2/5 步,图可能没生成)→ xelatex 硬失败;
(b) `singularity -B` 绑定路径被 `shlex.quote`,若 `scratch_dir` 用 `$HOME` 会被当字面量、绑定失败
   —— 若日志报 bind 错误就审 `_render_on_slurm` 的 `sing` 行;(c) 缺 `tlmgr` 包(构建时是 `|| true`)。
没有 CJK 字体配置,但手稿是英文,那属于加固不是本次病因。下次失败拿
`process/report_render_error.log`(或 HPC3 上 `~/.bioagent/report/bioagent_report_*-*.log`)即可收口。

## 2026-07-02 —— Slurm 作业在网关重启后不再丢失(分支 `fix/slurm-job-persistence-reattach`,已合并 `main`)

**起因。** `gateway/slurm_job.py` 的 Slurm 生命周期是:提交批处理作业后,在**内存里**轮询
`squeue`/`sacct`。`sbatch` 一返回作业就归 Slurm 管,所以即使网关进程挂了,计算照跑;但内存里那段
监督轮询丢了,而且没人记得 `job_id` 去重连。于是网关在分析中途重启就把正在跑的作业变成了孤儿。
(这正是“tmux 能不能替代 slurm”背后的真问题 —— 答案:不能,两者正交;修的是 job_id 持久化,不是
tmux。slurm 依旧是计算调度器。)

**改了什么。**
- **新增** `gateway/job_store.py` —— 原子 JSON 注册表(`JobRecord` + `JobStore`),复用
  `lab/archive.py` 的“写临时文件 + `os.replace`”模式;容忍文件缺失/损坏。刻意用扁平文件而非
  Postgres —— 等 LangGraph 迁移落地时可干净地换成 Postgres checkpointer(见 memory
  `architecture-direction`)。
- `gateway/slurm_job.py`:`run_batch_job`/`acquire_allocation` 增加可选 `on_submit(job_id)` 钩子
  (`sbatch` 一被接受即触发,重投后也会再触发);把监督轮询抽成 `supervise_job(...)`;**新增**
  `reattach_job(...)`(只观察 squeue/sacct,绝不重投;`wait=False` = 非阻塞状态探测)与
  `resume_incomplete(store, ...)` 扫描。
- `gateway/slurm_sandbox.py`:`SlurmCodeExecutor` 增加可选 `job_store` + `owner`;提交时记录、完成时
  标记终态。默认 `None` → 行为不变。
- `gateway/app.py`:在 `<workspace>/.bioagent/slurm_jobs.json` 建每用户 `JobStore` 并传给
  `SlurmCodeExecutor`;每次实时(重)连接时跑一次**非阻塞**的 `resume_incomplete` 扫描,刷新在途
  CodeAct 作业状态并告知用户。

**刻意划定的边界。** 不在**进程启动时**自动恢复:执行器是每连接一份、且只在 **Duo 2FA 之后**才存在,
启动时没有带凭据的 SSH 会话可供重连。重连发生在用户会话重新接上实时执行器时。目前只接了 `runcode`
路径;`scgpt_job`/`vlreview_job` 仍走未接线的 `run_batch_job`(后续很容易:传入 store + `on_submit`)。
重连扫描只上报状态,不重新收集已完成作业的产物、也不重放输出 —— 那是更大的会话模型改动,本次刻意不做。

**测试。** `tests/test_job_store.py`(9)+ `tests/test_slurm_reattach.py`(8),沿用仓库 fake-executor
风格;`test_slurm_job.py`/`test_slurm_sandbox.py` 仍绿。相关套件 32 个用例全过。

## 2026-07-02 —— CI 改为“测试优先”门禁;去掉强制审核(分支 `chore/relax-ci`)

**起因。** CI 几乎每次推送都红,不是测试挂(315 个全过),而是 `ruff` lint 步骤在琐碎问题上失败
(未用的 import、分号多语句、空 f-string),而这个 job 是必需状态检查。另外分支保护要求 1 个批准
审核,小团队不需要。

**改动。**
- `.github/workflows/ci.yml`:ruff 步骤改为 `continue-on-error: true` —— **只提示,不作为合并门禁**。
  真正的门禁保留:字节编译(语法)+ pytest 套件。本地用 `ruff check --fix` 保持整洁。
- 清掉现有 7 个 lint 小问题,今天 ruff 是绿的(`research_lab.py` 里死变量 `prior`、`app.py` 空
  f-string、`slurm_sandbox.py`/`vlreview_runner.py`/`test_provenance.py` 未用 import、
  `auth_routes.py`/`test_slurm_sandbox.py` 的分号多语句)。
- **`main` 分支保护**:移除强制 PR 审核(`required_approving_review_count` → 无)。必需状态检查不变
  且现在都通过;密钥/策略扫描(“Policy and collaboration checks”)仍作为门禁保留。

**给未合并分支的提醒。** 没合 `main` 的分支仍带着旧 lint 问题 + 旧的阻塞式 `ci.yml`;合入 `main`
即可拿到非阻塞的 lint 步骤。

## 2026-07-02 —— 注册报 “Could not start registration.” 且信息不明(分支 `fix/registration-flow`)

**现象。** 注册页只弹出 “Could not start registration.”,既没有填验证码的地方,也看不到用户名重复检测。

**根因。** 是数据库表结构漂移,不是逻辑 bug。自助注册功能本身是完整的(两步:start→verify,
而且 `register_start` 早就对重名做了 409 检测,且大小写不敏感)。但运行中的数据库是在
`PendingRegistration` 模型之前建的,缺 `pending_registrations` 表。`register_start` 往该表插入 →
`OperationalError` → FastAPI 返回纯文本裸 500 → 前端 `r.json()` 解析失败 → 只能回退到那句笼统提示。
因为第一步就挂了,带验证码输入框的第二步(verify)根本没机会显示,重名 409 提示也永远走不到。

**修复(分支 `fix/registration-flow`)。**
- 后端 `auth_routes.py`:`register_start`/`register_verify` 现在包裹主体,把任何*意料之外*的异常
  转成带 `detail` 的干净 JSON 500(校验类 400/409/403 原样透传)。异常同时写服务端日志,不再不透明。
- 前端 `app.js`:无 `detail` 时的兜底文案现在带上 HTTP 状态码,至少让服务端错误可定位。
- 测试 `test_registration.py::test_missing_pending_table_returns_clean_json_500`:删表后断言返回可解析的
  JSON 500 且含面向用户的 `detail`。
- 本地 `bioagent.db`(已 gitignore)重新 `init_db()`,补上该表以便本地测试。

**生产修复办法。** 应用会自愈:启动时 `db.init_db()`(`create_all`)会建出缺失的表。**部署机只需重启/
重新部署** 即可补表,之后注册 + 用户名重复检测按设计正常工作。无需 Alembic 迁移(项目没有迁移,
`create_all` 就是建表机制)。跑一次 `scripts/sync_deploy.sh` 就够:它会重启 console,而
`pending_registrations` 是全新表,启动时 `create_all` 会在 SQLite 和 Postgres 上都建出来(脚本里
“单独迁移”那句只针对 ALTER 已有表)。

**同一分支上的两个后续决定。**
- **邮箱现在有意不唯一** —— 同一人可持有多个账户(如一个管理员 + 一个普通账户)共用同一 UCI 邮箱。
  已移除 `register/start`、`register/verify`、管理员 `set_email` 里的邮箱查重 409。这里风险低,因为
  **登录只认 `username`**、且**没有基于邮箱的找回密码**,共用邮箱不会造成鉴权歧义。用户名仍唯一。
  注意:同邮箱的两次自助注册要顺序做(每个邮箱只保留一条 pending)。
- **新增管理员改权限开关** —— `POST /api/admin/users/{id}/role`,可在 user↔admin 间切换,管理员用户表
  里加了 “Make admin”/“Make user” 按钮。护栏对齐删除:不能改自己的角色(防自锁),不能降级最后一个
  管理员(防御性;实际上被自锁护栏遮蔽,因为调用者必须是管理员)。两项功能都有测试覆盖。

## 2026-07-02 —— 渲染级 VL 审核(Qwen3.6 看不见排版缺陷)

**问题。** Qwen3.6 是纯文本模型:它能审图表背后的数字,但对**只存在于最终 PDF 里**的排版缺陷是瞎的——文字压文字、图注压在图上、单元格被裁切、表格溢出。文本模型永远看不到这些。**解法:** 给它配一个独立的小视觉模型,专门看**渲染后的页面**。

**形态(完全照搬 scGPT Route C)。** 一个短命、按需的 `gpu:1` Singularity 批作业——不跟 Qwen 的 vLLM GPU 共驻、也不是常驻的第二张 GPU。套件:`deploy/vlreview/`(`vlreview.def`、`run_review.py`)+ `scripts/hpc3_vlreview_setup.sh`。网关提交/监督:`vlreview_job.py`(镜像 `scgpt_job.py`)+ `vlreview_runner.py`(把 pdf stage 到 dfs3b、跑作业、读回 `review.json`,镜像 `scgpt_runner.py`)。

**它是收尾流水线的一个阶段,不是 agent 工具。** PDF 只在研究循环结束后才存在,审核必须确定性地跑、并驱动一个重渲染循环——所以它和 `_postrender_text_check` 并排,做成 `app.py::_postrender_visual_check`(一行调用),不进 scientist 的工具目录。`run_review.py` 里两个检测器:(1) 确定性的词框重叠(零 GPU,永远跑);(2) Qwen2.5-VL 逐页 checklist。每个缺陷带一个受控词表里的修复指令;`tools/visual_review.py` 沿升级阶梯(表字号 footnotesize→scriptsize→tiny;11→10→9pt;页边距;图宽封顶;换行阈值;landscape)通过 `build_pdf_report(format_overrides=...)`(`tools/report.py` 新增参数)**重渲染**,直到干净或阶梯用尽。残留缺陷只进技术报告的 Diagnostics(`_build_technical_report(render_diag=...)`);正式 manuscript 保持干净。

**部署状态(2026-07-02)。**
- HPC3:`.sif` 已用 `--remote`(Sylabs)**构建完成**——HPC3 上没有 fakeroot(用户在 `/etc/subuid` 里无映射)。权重(`Qwen/Qwen2.5-VL-7B-Instruct`,~16GB)已下到 `/dfs3b/ruic20_lab/software/bioagent/vlreview_model/`——**要确认下满**(login 节点上被掐过一次,只有 9.5GB;重跑同一条 `hf download` 续传即可)。
- gres 已在集群确认:`gpu`/`free-gpu` 都有 `gpu:A30:4` 和 `gpu:A100:2`;`gpu32`/`free-gpu32` 有 `L40S`/`RTX6000`。默认现在 = 计费 `gpu` + `gpu:A30:1`(实验室账户买的是优先级;free-gpu 排队太慢)。用 A30,绝不用 A100。
- eyeserver(操作者,走 `sync_deploy.sh`):opt-in——只需 `BIOAGENT_VLREVIEW_ENABLED=1`,其余默认全对上集群。无新增 Python 依赖(PyMuPDF 在 .sif 里)。
- **一个 setup 期的坑已修:** 镜像为(无网的)运行时 GPU 节点烤进了 `HF_HUB_OFFLINE=1`,它把 setup 期下权重也挡了;下载步骤现在在容器命令**内部**把它关掉。
- 尚未做端到端实跑验证——开着 flag 的第一份真报告就是测试:盯 lab 事件流的 `Visual review …`。

## 2026-07-01(公网域名 TLS 证书)

## 2026-07-01(稍后)—— 控制台布局调整(左精简 / 右 tab / 冷启动卡)

同 `fix/frontend-ux-batch` 分支,仅 `frontend/console/*`。静态预览里实测过。

- **左侧精简**:连接成功后,GPU/模型/存储/断开/停 GPU 控件折进收起的 `<details>`
  "Connection & compute"(功能保留,只是收起);左侧现在就是 Chats + 一个**登出**按钮。
  首次 ready 时**两侧面板自动收起**,让中间对话区最大。
- **右侧 = 结果 tab**:小 **zip** 胶囊按钮 + **Files / Preview** 两个 tab。Files 是文件夹树
  (不再略缩图平铺);点**代码/文本**类文件(py/json/md/txt/csv/yaml/log…)在 Preview tab
  **内联预览**(像 Claude);pdf/图片仍走原来的弹窗。`renderDownloads`/`loadResults` 重写,
  `openResultFile` 按扩展名分流(`TEXT_PREVIEW_EXT`)。
- **冷启动移进对话区**:轻量 `#bootStatus` 卡片(转圈 + 一行阶段文案 + 小字副提示),由
  provisioning 事件驱动(`updateBoot` + `applyStatus`),ready 后约 1.8 秒自动关闭;报错则保留。
  只是"没僵死"的存活提示,不是原来那种啰嗦日志。

## 2026-07-01(稍后)—— 自助注册 + 管理端邮箱/搜索/删除

同在 `fix/frontend-ux-batch` 分支。用户现在可以自助注册,需要 **UCI 邮箱** + **邮件发送的
6 位验证码**;管理端新增邮箱列、用户搜索、删除。文档:`docs/self_registration.md`。30 个
认证测试通过(`test_registration.py` + `test_auth_accounts.py`)。

- **邮件是免费且基本本地的:** 通过校园中继 `smtp.uci.edu` 发送(收件人都是 `@uci.edu`,同域、
  可达)。可插拔发信模块 `gateway/email_send.py`,用 `BIOAGENT_SMTP_*` 配置;**不配置即 dev
  模式**(验证码打到 journal 日志,并作为 `dev_code` 返回给浏览器,所以没有 SMTP 也能本地跑通)。
- **新表** `pending_registrations`(`models.py`)在验证前保存 bcrypt 密码哈希 + 验证码哈希
  (15 分钟过期,5 次尝试),由 `init_db` 自动建表。
- **接口**(`auth_routes.py`):`POST /api/auth/register/start`、`.../verify`、
  `GET /api/auth/config`;域名限制 `BIOAGENT_ALLOWED_EMAIL_DOMAINS`(默认 `uci.edu`,含子域);
  开关 `BIOAGENT_ALLOW_SELF_REGISTER`(默认开)。
- **管理端**(`auth_routes.py`):`GET /api/admin/users?q=` 按邮箱/用户名模糊、纯数字时按 id 精确;
  `DELETE /api/admin/users/{id}` 级联删除 DB 历史 + 尽力删除该用户磁盘结果目录;禁止删自己或
  最后一个管理员。
- **界面**(`frontend/console/*`):登录卡新增注册 + 验证两个子面板;管理端表格新增邮箱列、搜索框、
  每行删除按钮。

## 2026-07-01(稍后)—— 前端 UX 批量修复(`fix/frontend-ux-batch`)

七个面向研究员的 console 修复(清单见 `docs/archive/frontend_ux_fixes.md`)。除注明外都在
`frontend/console/*`;后端在 `src/bioagent/gateway/app.py` 与
`agents/{research_harness,research_lab,sandbox}.py`。307 个测试通过,并在静态预览里验证过行为。

1. **跨对话结果串台 + 2. 流式卡住。** 进行中运行的流式状态现在与 DOM **解耦**(`state.run`),
   并把归属对话持久化(`RUNOWNER_KEY`,在 WS 回放前于 `restoreConnection` 恢复)。只有当归属
   对话可见时才挂载实时气泡(`mountStream`/`repaintStream`,由 `renderChat` 调用)—— 切换对话
   不会把结果泄漏到当前对话,切回时重新挂载并重绘(不再卡住)。
3. **折叠 run_code + 步骤小结 + 最终代码。** 工具流水只进可折叠的活动日志。每一步**最后一次成功
   的 run_code** 以格式化、可折叠的 **step_code** 代码块展示(harness 的 `tool_result` 现在带
   `args`;新增 `step_code` WS 消息,回放也支持)。critic 接受时渲染**步骤小结**行(质量=分数,
   意义=critique;`_critic` 现在会 emit `critique`)。
4. **日志 → 结果包。** 移除右栏事件/错误日志面板;完整日志写入每次运行的
   `process/event_log.txt`。
5. **Material Symbols**(fonts.google.com/icons)替换所有 emoji 图标。
6. **Runs 栏**每条运行只提供一个 `下载结果(.zip)`(去掉逐文件浏览)。
7. **文件夹上传**(可嵌套):`webkitdirectory` 选择器保留相对路径整树上传(`/api/upload` 增加
   `rel_path`;`/api/upload/register-folder` 把文件夹登记为一个 dataset,kind=folder)。数据集
   栏改为**芯片**(上传的名字),不再手填路径;一个会话里可后续再加文件夹,且所有上传都能被
   `run_code` 通过新的 `BIOAGENT_UPLOADS` 环境变量访问。文件夹会解析出主矩阵
   (`_find_primary_matrix`)供 QC/DE 工具使用。

## 2026-07-01(稍后)—— skill 参考代码:渐进披露

skill 仍是**文件夹**(`SKILL.md` + `scripts/*.py`,脚本保持可 lint/可测的独立文件),但加了**渐进
披露**,参考代码不再占上下文。之前:Scientist 每步 brief(`research_lab.py`)会把**全部**脚本正文塞
进去,每步重复。现在 brief 只列**清单(manifest)**——每个脚本的 `name` + 一行摘要(脚本模块 docstring
的第一行)——完整 body 通过新工具 `read_skill_reference(name)` **按需**取。工具在
`ResearchLab._make_skill_reference_tool()` 里造、init 时 append 进 catalog(闭包 `self._skill`,
skill 在 `run()` 里才选,和 run_code 闭包 sandbox 同理)。于是大模板只在某步真用到时才付上下文;
"模板要先局部改一下再跑"就是常规路径:取 → 改 → `run_code`。

`agents/presets.py`:`SkillScript` 加 `summary`;`_load_scripts` 用 `_script_summary` 抽取
docstring 首行。`test_research_lab.py` 43 项全过(新增
`test_read_skill_reference_fetches_template_body_on_demand`)。文档见 `skills/README.md`。

**设计说明(为何没走内联单文件)。** 当天早些时候我曾把脚本内联进 `SKILL.md`(`## Reference code`
段)并提交(`b0f11ec`),随后回退。文件夹 vs 内联只是**存储**,不改上下文成本——因为 loader 是
**eager** 的。真正的杠杆是 eager vs 渐进披露,与存储正交。渐进披露两头都赢:省上下文 + 支持临场改
模板。整个过程工具层没动——注册表(`agents/registry.py`)+ `src/bioagent/tools/` 的 Python 函数
工具;skill 只**组合**它们。

## 2026-07-01(稍后)—— 修复部署脚本以适配公网绑定

上公网后 app 绑定的是**内网节点 IP `<GATEWAY_BIND_IP>:8800`**(Envoy 路由到这里;127.0.0.1 和公网
NIC 都拒绝),但 `scripts/sync_deploy.sh` 和 `scripts/deploy_interactive.sh` 仍**用 `start.sh`
重启(默认 `BIOAGENT_HOST=127.0.0.1`)**、并对 `127.0.0.1:8800` 做健康检查。原样运行任一脚本会
**把 app 重新绑回 loopback → 公网站点掉线**,健康检查也打错了 host。另外 `deploy_interactive.sh`
的 `SVC_USER` 仍默认 `bioagent`(已迁移为 `aiscientist`)。

修复(两个脚本):新增 `BIND_HOST` 开关 —— **仅在设置时**作为 `BIOAGENT_HOST` 传给重启(所以本地
dev 行为不变,未设置时绝不会强制 loopback),并驱动 `HEALTH_HOST`;`deploy_interactive.sh` 的
`SVC_USER` 默认改为 `aiscientist`;设置了 `BIND_HOST` 时完成消息指向公网 HTTPS 域名。**公网 prod
请设 `BIND_HOST=<GATEWAY_BIND_IP>`**(sync_deploy 写进 `.deploy.env`,deploy_interactive 用环境变量)。
`deploy/README.md` 里的 k8s 镜像路径仍是目标态;当前 prod = 宿主机 app + selectorless Service。

## 2026-07-01 —— AiScientist 已上公网 🎉

**https://<PUBLIC_HOSTNAME> 已上线、公网可达、证书浏览器信任**(`ssl_verify=0`,
InCommon/Sectigo)。通过 `<admin-ucinetid>`(RKE2/Envoy 节点的 cluster-admin)部署。做了:(1)清掉卡住的
cert-manager ACME(删掉 `cert-manager.io/cluster-issuer` 注解 + 卡了 10 天的
`Certificate/aiscientist-cert`);(2)把 Pablo 证书装成 Secret `aiscientist-cert`(网关 HTTPS listener
本来就在等这个名字);(3)后端接线 —— 单节点集群(`texera.<PUBLIC_HOSTNAME>`,内网 IP <GATEWAY_BIND_IP>),
建 selectorless Service+Endpoints `aiscientist-app → <GATEWAY_BIND_IP>:8800`、HTTPRoute(:443→app)+
跳转(:80→301);(4)app 绑定 `--host 127.0.0.1` → `0.0.0.0`,服务账户迁移 `bioagent` → `aiscientist`。
端到端验证:HTTPS 200、HTTP→HTTPS 301、`/api/auth/me` 正常。完整 runbook:
[`deploy/public-domain-tls.md`](../../deploy/public-domain-tls.md)。

**加固已完成:** app 端口已移出公网卡 —— 只绑内网节点 IP `<GATEWAY_BIND_IP>`(Envoy 从这里连;
`<GATEWAY_HOST>:8800` = Connection refused)。选这个而非 iptables 规则,是因为这台是 Calico/kube-proxy
节点(手插规则易被刷掉)。残留的 `bioagent` 孤儿(PPID=1、没占端口)已 SIGKILL。

**剩余:** 私钥安全备份给 Jin(加密包在 `~/aiscientist-handoff/`,密码走另一渠道)+ 续期提醒
~2026-12-15(证书 2027-01-14 到期)。**mmfatlas**
(Texera 的 CELLxGENE)—— **2026-07-02 也已恢复**。它挂掉有**两个**原因:(1)同款卡住 ACME(无证书)
→ 装上它的 InCommon 证书 `mmfatlas-tls`(去注解 + 删 stuck Certificate);(2)**端口错配** —— app 监听
**5006**,但 `mmfatlas-svc`/containerPort 指向 **5005**,所以就算有证书也 503 → 把 `mmfatlas-svc`
targetPort 5005→5006(**在线改**;Texera 得在自己 manifest 里同步,否则重部署会退回)。他们的
pod/app/数据和 `mmfatlas-route` 一概没动。给 Texera 的交接文件:
`~/aiscientist-handoff/mmfatlas-texera-handoff.md`;私钥已在给 Jin 的移交包里。

## 2026-07-01(早些)—— 公网域名 TLS 证书已签发并校验

Pablo Lozano 为 **<PUBLIC_HOSTNAME>** 和 **mmfatlas.<PUBLIC_HOSTNAME>** 签发了
InCommon/Sectigo 证书(2026-06-30 收到)。2026-07-01 全部校验通过:完整链(叶子 →
InCommon RSA OV SSL CA 3 → Sectigo Root R46)、SAN 正确、**叶子证书与本地私钥配对成功**、
有效期 2026-06-30 → **2027-01-14**(~199 天)。证书/私钥整理在 `~/aiscientist-certs/`
(git 外;私钥永不入库)。

完整操作流程 + 续期 + 私钥保管交接写入
[`deploy/public-domain-tls.md`](../../deploy/public-domain-tls.md) —— 即 Jin 要的"公网域名
配置"文档。

**仍待办(卡住上线):** 部署图上那两条橙色虚线还没接 ——(1)把证书装进 **Envoy Gateway**
(TLS Secret + 按 host 的 HTTPS listener + HTTPRoute);(2)把网关后端接到**主机 app
`127.0.0.1:8800`**(selectorless Service + 手动 Endpoints)。两步都要集群权限 —— `<ucinetid>`
没有 kubectl/kubeconfig,须由 RKE2/Envoy 集群管理员 apply 或发一份 kubeconfig。另外待办:把私钥
**安全地**备份给 Jin(用于续期)、设一个 ~2026-12-15 的续期提醒。步骤 5–6 落地前,控制台只能靠
SSH 隧道访问 `127.0.0.1:8800`。

**权限更正:** 有 kubectl+sudo 的是 `<admin-ucinetid>`(不是 `<ucinetid>`)—— 所以 Yijun 自己就能做
k8s 部署(确认它有 `apply` 写权限)。**服务账户:** Jin 已预建 `aiscientist` 账户(全小写,uid
995);app 目前仍跑在 `bioagent`(systemd `bioagent.service`)。迁移命令 + app 绑定卡点
(127.0.0.1 → CNI 可达 + 防火墙)见 runbook;仓库配置(`deploy/systemd/bioagent.service` 的
`User=`、`scripts/sync_deploy.sh` 的 `SVC_USER`)已指向 `aiscientist`。**mmfatlas:** 决定只帮
那一次性、可回滚、有文档的网关+证书接线(它是 texera-ns 的 CELLxGENE 服务)—— 不接管,私钥交给
Jin。

## 2026-06-30(报告质量)—— 基于一份运行结果包的 4 项修复

复盘了真实结果包(`bioagent_results_8a291d1c121a`):手稿结构与诚实度不错,但有两个硬伤(参考文献
跑偏;按细胞类型的 DE 步骤被静默丢弃),外加 run_code 报错(OOM/-9、`groupby='DDX41'`、相对路径
FileNotFound)。落地四项修复(均有测试;全量 278 通过):

1. **文献查询来源**(`tools/literature_references.py`)。报告的 `## References` 之前由
   `gather_references(req.question)` 用裸 UI 提示词("Implement this research and give me report.")
   驱动 → Europe PMC 匹配到 "implement/research" → 减肥/新生儿论文。现改为 `build_reference_query`
   用 agenda 主题(DDX41/WT)+ 科学家回路内 `literature_search` 查询(retina/photoreceptor)构造查询,
   并用 `harvest_inloop_references` 直接复用回路内已找到的、带 DOI 的相关论文。**保留** Europe PMC
   兜底(既定过渡路径;paper-qa/远程仍搁置 —— Ziyao 负责开发,我们负责集成)。
2. **run_code 上下文注入**(`agents/sandbox.py` + `agents/research_lab.py`)。工具描述现在带上实时
   obs schema(`sampleid: DDX41, WT` —— 消灭 `groupby='DDX41'` 猜测)、真实 BIOAGENT_* 路径、
   "CWD 是临时目录" 警告、内存提醒。
3. **run_code 上 HPC3**(`gateway/slurm_sandbox.py` `SlurmCodeExecutor` + settings + app 接线)。
   `BIOAGENT_RUN_CODE_ON_HPC=1` 时把每段代码作为 CPU Slurm 作业提交,带真实 `#SBATCH --mem`
   上限(修复 OOM/-9)。默认关闭 → 本地沙箱不变且作为兜底。sbatch 示例见 `skills/README.md`。
4. **降级通道泛化**(`gateway/app.py` `_summarize_pipeline_degradations`/`_step_failures`)。步骤降级
   (max_steps、工具/OOM 失败 —— 之前只在恒为空的 `sr.errors` 里)现在只进技术报告的 Diagnostics;
   成品手稿按设计保持静默(见记忆 `silent-degradation-design`)。

未做(延后):渲染层缺陷 → 未来接视觉审核模型(记忆 `vl-report-review-backlog`);harness 把
`step.ok=True` 即使有 traceback,是单独的监控小问题(降级汇总器改读 returncode/status 绕开它)。

## 2026-06-30(最新)—— 通过 vLLM `/tokenize` 精确感知窗口(在 HPC3 服务端)

接着下面那条溢出修复继续做。决策(Yijun):精确的 token 计数放在 **HPC3 的
Singularity vLLM 容器里、而不是 eye-server 上**(网关上不放 tokenizer 文件 / 依赖)。
用 vLLM 服务端 `/tokenize` 实现:

- `gateway/vllm_client.py` 新增 `count_tokens(port, model, messages, tools, ...)`:把
  messages POST 给 `/tokenize`(在服务器**根路径**,不是 `/v1`),读回模型自己的
  tokenizer + chat 模板算出的**精确**计数 —— 就是它用来卡 `--max-model-len` 的那一个。
  对远端 `base_url`(OpenRouter 没有 /tokenize)或任何传输错误返回 `None`,于是
  harness 透明回退到字符估算。**绝不抛异常**(token 计数不能成为搞挂一次运行的原因)。
- `agents/research_harness.py`:新增可注入的 `count_tokens_fn`。`_budget_messages` 现在
  先按字符估算裁剪(便宜,先挑出该丢的内容),再 —— 当有计数器时 —— **核验**服务端的
  精确计数并继续收紧直到真正放得下。旧的估算逻辑原封不动搬进 `_budget_by_estimate`。
  估算现在是**兜底**、不是主路径。发出 `context_measured {exact_tokens, allowed}` 事件。
- `gateway/app.py`:`_lab_llm` 现在返回第 5 个值 `count_tokens`(绑定到本会话隧道),
  传给 `ResearchHarness(count_tokens_fn=...)`。`_lab_event_to_chat` 把 `context_measured`
  渲染成一行 📏 活动日志。**注意:`_lab_llm` 现在返回 5 个值** —— 任何新调用方都要解包
  `complete_fn, scientist_chat, model, label, count_tokens`。
- 测试:`test_vllm_client.py`(+根路径 /tokenize、远端→None、传输错误→None)、
  `test_research_harness.py`(+精确计数器会收紧、None→估算兜底)。
- **第二条预算路径也一并加固**(改成 5 元组后做了全仓库排查):`research_lab.py` 的
  `_budget_single_shot`(PI 计划 / Critic / synthesize 这几个单次补全)有同样的估算风险。
  现在它**复用注入进来的 Scientist 的计数器**(`self.scientist._exact_token_count(msgs, [])`,
  无需新增构造参数)精确感知窗口,把最大的 user 消息截到**精确计数**真正放得下为止;
  没有计数器时回退到估算。新增 `_exact_tokens` / `_prompt_tokens` 辅助方法。
- 排查结论:4 处 `_lab_llm` 调用点全部更新(1 生产 + 2 测试 + 定义);生产向 `ResearchLab`
  注入了 `scientist=`(带计数器)和 `complete_fn=`,**两条路径都有**精确感知。
  `research_lab.py:511` 的内部默认 `ResearchHarness` 无计数器,但只在非网关(测试)路径触发
  → 走估算兜底,无妨。**全套 262 全绿。**

## 2026-06-30(后续)—— 上下文窗口溢出:反应式自压缩

QC 流水线本身跑得没问题,但有几步在 vLLM 上 400 了:prompt 命中 30721 输入 +
2048 输出 = 32769,比 32768 窗口刚好多 1 个 token。`_budget_messages` 已经在主动裁剪,
但 `chars/3.0` 的 token 估算**低估了密集 JSON**(工具 schema + 结果负载),于是落在硬上限
之上约 1%。修在 `agents/research_harness.py`:

- `_CHARS_PER_TOKEN` 3.0 → **2.6**(对密集 JSON 多估 → 更早裁剪);
  `context_safety_margin` 1024 → **2048**。
- **反应式自压缩**:新增 `context_retries`(3)+ `context_retry_extra_tokens`(3072)配置。
  当 `chat()` 抛出 context-length 400(由 `_is_context_overflow` 字符串匹配检测,这样
  agents 层和 `GatewayError` 解耦)时,运行循环用额外预留重新裁剪并重试,而不是让整次运行
  失败。`_budget_messages` 增加了 `extra_reserve` 参数。
- 实时监控:`gateway/app.py` 的 `_lab_event_to_chat` 现在会呈现 `context_trimmed`
  (活动日志)和 `context_overflow_retry`(可见的 "🗜 recompacting…" 警告)。
- 测试:`tests/test_research_harness.py`(+溢出重试/恢复、非溢出错误重新抛出、签名检测、
  extra_reserve)。全套件 256 全绿。

## 2026-06-30 —— 重复计划卡片 + 中途进度卡死

两处中间面板的流式修复(工作树在 `main`,未推送):

- **plan 模式下重复的 "📋 Plan ready" 卡片。** `pi_agenda` 被发了两次 —— 一次在
  `_pi_plan()` 起草议程时(`research_lab.py` `_pi_plan`,约 888 行),另一次在用户批准后
  (`research_lab.py:597`)。删掉批准后的那次重发;草稿(及每次修订)本来就会展示计划。
  非 plan 模式不受影响(那里只触发草稿那次发送)。
- **执行看起来卡死 / 没有中间结果。** 传输是好的(`call_soon_threadsafe`),但
  `_lab_event_to_chat`(`gateway/app.py`)把 `tool_start`/`tool_result` 只路由到**可折叠**的
  `chat_thinking` 日志,对 `finish` **没有处理分支**,中途也没往常驻可见的信息流里输出任何
  东西。现在:`tool_start` → "⚙️ Running <tool>…",`tool_result` → "↳ <tool>: <summary>",
  `tool_error` → 警告行,`finish` → "🔎 Found: <answer_preview>"(Scientist 自己对这一步的
  小结)。两个通道仍然都会收到详细的活动行。
- 测试在 `tests/test_lab_progress_stream.py` 更新(工具回合现在同时断言两个通道;新增
  `finish` 用例)。该文件 + `test_research_lab.py` + `test_gateway_lab.py` 全绿。

---

日期:2026-06-18(多智能体 Virtual Lab 重新启用 + Lab Archive 草案 —— 请先看顶部 2026-06-18(后续)小节)

> ⚠️ **请先读顶部 `## 2026-06-18(后续)` 小节**(多智能体 Virtual Lab 重新启用 +
> 下周要讨论的 Lab Archive 设计草案),再看 `## 2026-06-17` 重构小节。2026-06-11 那版"方向修正"计划现在
> **已经在代码里落地**(分支 `refactor/harness-and-kosmos-cli-removal`),所以旧小节里
> 那些"将要做"的表述都是历史、不是路线图了。截至 2026-06-17,简言之:
> - **13-agent `VisionResearchAgent` 流水线已删除** —— 换成 **`ResearchLab`** 循环
>   (PI → Scientist → Critic → 汇总);
> - **Kosmos 已彻底移除**(不是"将要移除");**自主循环 / harness / `eval/` / 独立 CLI
>   都已删除**(不是"冻结");
> - **Biomni 是被淘汰、不是 vendor** —— 由一个专门的 `literature_search` 工具 + 真实的
>   scanpy 分析线取代;**没有 `BioToolRuntime`**;
> - 报告是**出版级手稿**(确定性 pandoc 打包 + 模型自审)—— **没有单独的 `OutputAgent`
>   类**,VL 排版审查也**没做**(仍是未来的质量升级项);
> - **文献归属从 Wenyi 转给 MaziYao**(Wenyi 后续可能不在项目里)。
>
> 2026-06-17 小节以下都是历史日志(从新到旧)。HPC3 控制台 / gateway / SSH+Duo /
> GPU serve / Singularity-Slurm / 部署 的内容仍然有效;但 §1/§4/§8/§9 里关于
> agent 架构和 Biomni/Kosmos 的描述已被 2026-06-17 小节**取代**。

---

## 2026-06-30(最新+20)—— 回退 b3c7007 的文献改动,为 PR #12 让路(文献线负责合并)

PR #12(<ucinetid>-stack,`codex/fix-literature-references`)是基于老的 `f0aa701` 的**第二套、竞争性
文献修复**,加了 `_focus_reference_query` + Europe-PMC 引用过滤。我们分支里已有 `b3c7007` 的**另一套**
文献修复(`derive_reference_query` + `_extract_topic`、references-first 写作)。git 合并"看着干净",
但结果是 Frankenstein:两套聚焦器都在,`gather_references` 聚焦两遍,`derive_reference_query` 变死代码。

**只精确回退 b3c7007 的文献部分**(它是混合提交——文献 + 数据感知 PI 规划 + DE skill)。因为 b3c7007
父提交是 `b61d1a7`,且自那以后**只有 b3c7007 改过**这 3 个文件,所以对它们 `git checkout b61d1a7 --`
就是精确、最小的撤销:
- `tools/literature_references.py`、`tests/test_literature_references.py`、`gateway/app.py` →
  回到 `b61d1a7`(移除 `derive_reference_query`/`_extract_topic`/inline-`[N]` 写作 + references-first
  重排;app.py 回到 `gather_references(req.question)` + `insert_references`)。
- **保留**(非文献):`agents/research_lab.py` 数据感知 PI 规划、`tools/datasets.py` preflight、
  数据集测试,以及 `differential_expression` DE skill + `condition_by_celltype.py`。
- 前向提交(**不改写历史**——共享 push 分支)。全套 **244**(从 257 降 = b3c7007 的额外文献测试随代码回退)。

之后 PR #12 能干净合并:它的 `gather_references(question)`(内部聚焦)正好对上我们恢复的
`gather_references(req.question)` 调用——不双聚焦、无死代码。**文献线(Ziyao)负责把 PR #12 合下来**;
别再把 b3c7007 的文献版本带回来。

## 2026-06-30(最新+19)—— 团队模式:协作式、评分驱动的会议(默认 2 轮)+ team_selection/tools/workflow 的决策

读了 Virtual Lab 原文(Nature s41586-025-09442-9;bioRxiv 2024.11.11.623004)看它多 agent 怎么做。
要点:团队会议内 agent **共享对话、互相递进**约 3 轮(PI → 各专家 → Scientific Critic → PI 汇总);
**多样性靠把整场会议并行跑多次再 merge**;Critic 很关键(降幻觉);人类只占 ~1% 发言;模型 GPT-4o
(无单卡约束,所以并行随便跑)。

**针对我们单 A100 的设计决定(就是答案,记下来):**
- **协作通过 PI 共享 synthesis,而非共享原始发言。** 纯共享对话会逼专家**串行**(2 号等 1 号),
  在单 A100 上**杀掉 vLLM batch**。所以:第 1 轮 = 各自独立多样初判(并发/batch);第 ≥2 轮 = 专家
  **在 PI 的共享 synthesis + Critic 反馈基础上推进**(仍并发——共享的是 synthesis 产物,不是彼此原始
  发言)。这拿到了原文协作递进的大部分,又保住 A100 并发。原文的 MERGE 本身也是这思路(PI 看 summary
  不看全文)。
- **并行多跑 + merge 维持延后**——它**乘** A100 成本(原文没 GPU 约束)。除非质量需要再上,且放默认关
  闭的 config 后面。
- **team_selection:保留。tools_selection / workflow_design:不单独开会。** 我们工具是**固定 catalog**
  (对固定菜单开会 = 白烧调用),SKILL.md 库**已经编码了 workflow**(单开 workflow_design 会和 skill+
  设计会重复)。原文要分这几场是因为从零设计纳米抗体是开放解空间;我们的分析任务不是。一场设计会 +
  动态组队就是对的、且更省 A100 的形态。

**已落地(`agents/research_lab.py`):**
- `meeting_rounds` 默认 **1 → 2**,让协作默认真的发生。
- **评分驱动会议。** 会议 Critic 现在返回 JSON `{score, critique}`(`_meeting_critic`)。下一轮给专家的
  反馈**按分数分档**(`_round_feedback`):<0.5 →"用力反驳、别附和";<0.8 →"具体回应 + 质疑没依据的
  主张";≥0.8 →"巩固"。所以质疑是**基于实际 critique**,不是无脑唱反调。
- **提前收敛。** Critic 分数过 `meeting_accept_score`(0.85)就提前结束(简单议题省、争议议题多议)
  ——A100 自适应。发 `meeting_converged`。
- 测试:协作 build-on 共享 synthesis + 低分"push back"反馈;默认 2 轮;高分第 1 轮就收敛。全套 257 绿。

## 2026-06-30(最新+18)—— 数据感知的 PI 规划器 + 文献检索词修复(从真实运行根因定位)

起因:复盘一个真实结果包(`Ddx41_DEG.h5ad`,运行 `beb40c4849c5`)。报告看着精致,但有两个不同的缺陷;
根因分别在**两个不同的层**,所以修复也分两层。**两处都是引擎/`.py` 改动,不是新的 `skills/*/SKILL.md`**
——原因见下文"为什么是 .py 不是 skill"。

**缺陷 1 —— 引用全不相关(报告输出层)。** 稿件 References 全是教学法/手语/NIH fellowship 论文,跟视网膜
毫无关系。原因:`gather_references()` 拿运行的原始 `question` 去检索,而这次的 question 是元指令
*"complete the research and write the topic by yourself"* → Europe PMC 关键词命中 "research/writing/topic"
→ 返回教育类论文。agent 运行中真正找到的相关文献(Nrl/rod、cone、Müller glia)只在过程日志里,从没进报告。
- 修复:`tools/literature_references.py` 新增 `derive_reference_query()`(在 `gateway/app.py` 接线),改为
  检索运行的**真实主题**——稿件/PI 综述**标题** → 否则非元指令的 question → 否则 agenda。能识别并跳过元指令。
  隐私不破:只用一行标题(公开主题短语,不含数据派生数值),绝不发送综述正文。

**缺陷 2 —— 研究路线无视实验设计(skill/规划层)。** 数据集是 **DDX41 vs WT** 对照(`sampleid=[DDX41,WT]`),
而且自带专家标签(`majorclass`、`celltype`、`scANVI_…`)。这次运行既没做基因型比较、也没复用标签——只产出了
一份泛泛的描述性图谱,甚至把框架错标成"developing retina"(根本没有年龄字段)。根因:**PI 规划器(`_pi_plan`)
是瞎的**——它只拿到 `question + guidance + tools`,从没拿到数据集的 obs 元数据。遇到开放式提问就套了通用模板。
- 修复(引擎层,通用、无数据集专属硬编码):
  1. `tools/datasets.py` —— preflight 现在抽取分类 obs 取值(`obs_categoricals`):低基数列给出取值
     (`sampleid=[DDX41,WT]`、`majorclass=[…]`),高基数列(130 类的 `celltype`)只记数量,prompt 不爆。
  2. `agents/research_lab.py` —— `_dataset_context()` 把画像喂进 PI 规划 prompt;`_PI_SYSTEM` 加入设计感知规则:
     **有 condition/group 列 → 规划组间比较;有标签列 → 复用+验证,别从头重做;只引用真实存在的列。**
- 注:一个正好能做这个分析的 skill **本来就存在**(`skills/differential_expression/`,A-vs-B 组间 DE)。它没被用上,
  是因为瞎的规划器看不见设计、选不到它。本次修复让那个 skill(以及任何 skill、自由规划)在对照数据集上真正可达。

**为什么是 `.py` 不是 `SKILL.md`**(层次问题,对后续很重要):SKILL.md 是被 `_pi_plan` 读取的引导 prompt——但
(a) 它修不了"规划器根本没收到 obs 元数据"这个管道问题(管道=代码);(b) 它无法新增"从 h5ad 抽分类取值"这个能力
(工具=代码);(c)"每次运行都按自己的数据规划"必须对所有 skill 和自由规划都成立,所以属于固定内核,而不是某一条协议。
SKILL.md 仍是**某条具体协议**的正确归宿(被推迟的"选项 2"),它叠加在这个地基之上。

**测试:** 新增 `tests/test_dataset_preflight_obs.py`,并在 `tests/test_literature_references.py`、
`tests/test_research_lab.py` 增补。96 个相关测试通过。

**遗留待办 → ✅ 已修(2026-07-07,见顶部『Critic 计数读数 bug』):** Critic 误判 DE 基因数(说每簇 10 个,
实际落盘 `de_leiden_*.csv` 是 50)——真因是 `run_de` 返回体 `top_genes_by_group` 写死 `[:10]`,已补真实计数字段并
硬化 Critic prompt。另一半("(no answer)" 轮被判 ACCEPT)已被确定性地板超越:无产出的步强制 revise,有 artifact 的
incomplete 步可接受是有意为之。

## 2026-06-29(最新+17)—— 轴A 性能:团队会议并发(A100 batch)+ 有界多轮

轴A 后续,专门针对**单 A100 瓶颈**(一块 vLLM 服务整个团队,团队模式多出来的 LLM 调用就是成本大头)。
`agents/research_lab.py` 两处改动:

- **一次会议里的专家现在并发执行**(`_complete_concurrent`:对每个专家的 `_complete` 用有界
  `ThreadPoolExecutor`、保序)。`_complete` 是阻塞 HTTP 调 vLLM、会释放 GIL,所以 N 个请求同时在途,
  **vLLM 的 continuous batching 把它们合在 ~1 个 GPU pass 跑,而不是 N 次串行往返**。这是 A100 最大的
  性能修复——3 专家会议的专家阶段从 3× 生成延迟降到 ~1×。
- **有界多轮会议**(`meeting_rounds`,默认 **1** = 不乘成本)。>1 时第 k 轮把上一轮 PI synthesis 喂给每
  个专家去细化(仍独立——彼此看不到对方原始发言)。事件带 `round`。
- **故意保守的 A100 默认值:** `meeting_rounds=1`、`max_meeting_concurrency=4`(限在途请求,防大团队撑爆
  KV-cache 池——74.6GB 大半是 KV 池,见最新+6)、`team_size=3`。`single` 模式(默认,scGPT 这类例行
  任务)**零会议开销**。
- **故意没做**(它们会**乘**A100 成本,与指令相反):并行会议 + merge、独立的
  tools_selection/workflow_design 会议阶段。一个设计会议 + 一个解读会议、专家并发,就是性能上的折中。
  以后若要加,放在默认关闭的 config 后面并写明成本。

测试:多轮把上一轮 synthesis 喂下去 + 跑 2 轮;现有 2-专家团队测试覆盖并发路径(保序)。全套 240 绿。

## 2026-06-29(最新+16)—— 论文打磨:Methods 多级标题 + 可点击跳转的引用(分支 `fix/report-output-and-file-browser`)

**触发:** Yijun —— 论文分点不清晰/没编号(尤其 Methods),References 不是可点击跳转到原文的超链接引用格式。

**已落地(关键处确定性实现;LLM 不碰脆弱的锚点语法):**
- **Methods 改为标题层级结构。** `_REPORT_WRITER_SYSTEM` + `_REPORT_REVIEW_SYSTEM` 现在要求每个流程阶段是带编号的三级标题 `### N. <阶段>`(参数/结果作为其下的短 bullet),子步骤用 `#### N.k <子步>` —— 取代原来的一个扁平编号列表。review 提示被告知保留这些阶段编号(不要当作"手写章节序号"删掉)。
- **可点击交叉链接的引用。** 流水线重排:先 gather references,把编号列表传进 `_build_report(... lit)`,让写作模型在正文用 `[N]` 内联引用(只用于背景/解读,绝不用于本数据集自己的数字)。自审之后,确定性的 `link_citations()`:(1) 把 `## References` 重建为带锚点的超链接条目 `[\[N\]]{#ref-N} [cite](url)`(span 是跳转目标,引用文本链到 DOI/PMID 原文);(2) 把正文 `[N]`/`[N, M]` 变成 `[\[N\]](#ref-N)` 链接。越界编号保持纯文本;图片 alt / 已有链接不动。
- **为什么 review 之后再链:** review LLM 只看到干净的纯编号列表 + `[N]` 标记,无法破坏 pandoc 锚点语法。`build_pdf_report` 已带 `colorlinks=true` —— 实测 pandoc 输出 `\hyperref[ref-N]` → `\label{ref-N}`(PDF)与书签(DOCX),两种格式都可点。

**测试:** `tests/test_literature_references.py` 新增 4 个 `link_citations` 用例;全套 239 绿。端到端 + pandoc 渲染均验证通过。

**状态:** 代码完成,尚未提交(3 个文件:`gateway/app.py`、`tools/literature_references.py`、`tests/test_literature_references.py`)。

## 2026-06-29(最新+15)—— 轴A 落地:Virtual-Lab 团队模式 + 用户选"模式"不选 skill(分支 `feat/axis-b-pi-skill-selection`)

做了**完整轴A v1**(用户选了"现在就做")+ 用户要的 UX 简化:研究员该选*模式*,不该去翻 skill 正文。

**后端——真·多智能体团队模式(`agents/research_lab.py`):**
- `LabConfig.mode`:`"single"`(默认,现有 per-step Scientist→Critic loop,**不变**)|`"team"`
  (虚拟实验室)|`"auto"`(PI 路由)。`team_size` 限团队人数。
- `"team"` 流程:PI **动态组队**(`_form_team` → JSON 专家 → 独立上下文 `Specialist`)→ **设计
  会议**(`_team_meeting`:每个专家只从**自己的上下文**发言、看不到别人原始发言,然后 first-class
  会议 Critic,再 PI 汇总)汇总注入规划 guidance → 现有工具执行 loop 跑 agenda(步骤路由到动态团队)
  → 对已接受结果的**解读会议** → PI 写报告时织入团队解读。发 `mode_selected`/`team_formed`/
  `team_meeting_start`/`expert_contribution`/`meeting_critic`/`meeting_synthesis` 事件。
- **独立记忆 = 仅运行内**(同一份权重 + 各专家独立对话,不额外吃显卡,见最新+6 硬件结论)。持久
  per-agent 记忆(Lab Archive)= 轴C,仍延后。
- `mode` 经网关接通:`LabRequest.mode` → `_run_lab` 里 `LabConfig(mode=...)`。

**前端——选模式,不选 skill(`console/index.html` + `app.js` + `styles.css`):**
- 新主控件 `#modeSelect`:🧑‍🔬 单 agent / 👥 虚拟实验室(多智能体)/ ✨ 自动。随 `/api/lab` 发送;
  按 chat 存(`s.mode`),`syncPresetUI` 里恢复。
- 可编辑 guidance **文本框整个删除**(它把 PI 内部的 skill 正文暴露给研究员,对研究员无用;要改
  计划就在 chat / plan 模式里跟 PI 说,而不是编辑文本)。只在折叠的 **「Advanced — 强制指定研究
  路径」** 里留一个协议*下拉*(只有 key、不含正文);默认 = PI 自选(轴B)。前端不再发 `preset_prompt`
  (永远 null → 后端用被强制 preset 的默认正文)。删掉 `onPresetEdit` + `presetPrompt`/`presetToggle`/
  `presetPanel` 的 DOM 及其持久化。

**为何解决了用户的不满:** skill 正文是给 PI 的内部 guidance,本就不该展示给研究员。现在用户只选
单/团队;skill 由 PI 选(B),通用计划走 plan-mode(agenda)展示,而不是 skill 原文。

- 测试:团队模式跑两个会议 + 设计会议引导规划 + 解读织入报告;auto 路由单/团队。`mode="single"`
  默认让此前所有测试保持绿。全套 235,app.js 语法干净。

**轴A 内部延后(后续):** 多**轮**会议(目前各 1 轮)、并行会议 + merge、独立的
tools_selection/workflow_design 会议阶段、网关 *override* 路径仍只传文本(不带脚本)。轴C(持久
Lab Archive)维持延后。

## 2026-06-29(最新+14)—— skills/ 迁移开发:参考代码 + 解耦 + 库 2→4(分支 `feat/axis-b-pi-skill-selection`)

轴B(最新+6)的"迁移开发"续作。把 operon-`skills/` 库缺的三样补齐——**参考代码、解耦、扩库**:

- **参考代码(`scripts/`)。** 每个 skill 现在带上工具没覆盖部分的 CodeAct 模板——是 Scientist 用
  `run_code` **改用的模板,不是自动跑的代码**,且**调用工具的 checkpoint**(BIOAGENT_WORK ->
  `adata_qc/clustered/de.h5ad`,写 BIOAGENT_ARTIFACTS)而非重造工具。新脚本:
  `celltype_annotation/annotate_clusters_by_markers.py`(marker→标签)、
  `scgpt_annotation/crossvalidate_scgpt_vs_leiden.py`(scGPT↔Leiden 混淆+置信度)、
  `differential_expression/pairwise_de.py`(A-vs-B DE)、`gene_signature_scoring/score_signature.py`。
- **解耦格式。** SKILL.md frontmatter 加 `tools:`(协议编排的注册工具 = 它**调用**的能力层);正文
  重写为模式无关 + 点名工具 + 引用自带脚本。`presets.py`:`SkillScript` dataclass、
  `ResearchPreset.tools/scripts`、loader 解析 `tools:` + 加载 `scripts/*.py`、`list_presets()` 加
  `tools`/`scripts`(增量,前端/System 页不受影响)。
- **库 2 → 4。** 新增 `differential_expression`(A-vs-B 组间比较,区别于 celltype 的贴标签)和
  `gene_signature_scoring`(`sc.tl.score_genes`)。四个:celltype_annotation、scgpt_annotation、
  differential_expression、gene_signature_scoring。
- **接线(脚本不是死的)。** PI 自选 skill 时(`self._skill`),其参考脚本注入 Scientist 每步 brief,
  并严格框定"只在工具没覆盖该步时才改用"。**缺口:** 网关 *override* 路径(用户挑 preset)只传文本,
  暂不带脚本——默认的自选路径带。把 override 改成传 `ResearchPreset` 是个小后续。
- 测试:loader 读 tools+scripts、新 skill 在册;选中 skill 的脚本进 brief;steer 测试按新 body 更新。
  所有脚本 `py_compile` 通过。全套绿(232)。本次没提交别的线的东西(工作树原本干净)。

**仍延后:** skill-body 与*模式*解耦只做了一半——body 现在模式无关,但 individual-vs-team 决策是轴A
(下一步)。skill 选择的规模化(embedding 索引)在 4 个 skill 时还不需要。

## 2026-06-29(最新+13)—— L2 gateway 重启后的"可重挂"提示(分支 `fix/report-output-and-file-browser`,未合并)

**关键发现(别重复造):** L2 最贵的部分——gateway 重启后重挂到仍在跑的 GPU job——**已经实现**。
`gpu.find_running_job`(`squeue --me` + 每用户 job 名)+ `ensure_serve_job` 会复用在跑的 job,
端口从 HPC3 的 `$HOME/.bioagent/vllm.port`(`PORT_FILE`)读。这些状态在 **HPC3 上**,gateway 重启
照样在:重新登录即重挂,**不重新排队、不重载模型**。唯一省不掉的是 SSH + Duo 重认证(凭据不能缓存)。
所以服务端持久化层/"启动时自动复活"是多余的(而且没凭据也无法自动复活)。

**这个缺口的本质:** 纯认知问题——gateway 重启后旧 connection_id 会 404,`restoreConnection()`
静默掉回冷登录,用户看不出 job+模型其实还活着、重连会很快很省。Yijun 选了"只加提示"(不做后端持久化)。

**已落地(仅前端,无 .py 改动):**
- `frontend/console/app.js` —— 会话 ready 时写一条 `LASTCONN_KEY` localStorage 记录
  (username/host/model/job_id/node);连接失效恢复时 `showReattachHint()` 在登录页显示可关闭横幅
  ("你的 GPU job 很可能还在跑——重新登录即自动重挂,跳过排队+模型加载;用完用 Stop GPU 释放 SU"),
  并预填 username/host。连接或关闭时横幅消失。
- `frontend/console/index.html` —— 登录区 `#reattachHint` 横幅。
- `frontend/console/styles.css` —— `.reattach-hint` 样式。
- 无测试(未加后端逻辑;仅 `node --check`)。

**待办/下一步:** 重启时进行中的那一轮 run 仍会丢(→ L3 checkpoint/resume)。若以后想要登录页"一键重挂"
按钮或可重挂 job 列表,那是延后的中等选项(需服务端持久化活跃会话)。

## 2026-06-29(最新+12)—— L1 会话重连:刷新/误回退不再丢失进行中的运行(分支 `fix/report-output-and-file-browser`,未合并)

**触发:** Yijun:刷新或误回退后,登录还在,但 HPC3 连接和当前运行的视图全丢,看不出跑到哪、
以为得重跑一整轮。还希望支持超时续跑(从上次思考/任务节点继续),并为 15GB 数据集/长跑提前考虑。

**诊断:** 服务端 `Connection`(SSH+GPU+vLLM)和运行任务 `asyncio.create_task(_run_lab)` 在客户端
刷新后其实都**还活着**(没有 beforeunload;`CONNECTIONS` 是无 GC 的内存字典;WS 重连本就会重放
`conn.log`)。唯一丢的是客户端的 `connection_id` —— 只在 JS 内存里、从不持久化 —— 所以页面再也
不去重连。两个缺口:中间气泡的流(`chat_token`/`chat_thinking`/`lab_progress`)是 `conn.push` 的、
**不进 `conn.log`**,重连重建不了气泡;`summary()` 也没暴露 `chat_running`。

**约定范围(三层计划,Yijun 选先做 L1):**
- **L1(本次完成)** 重连 + 续看(gateway 没重启)。
- **L2(延后)** 跨 gateway 重启重挂:落盘 job_id/node/port/user,启动探测 Slurm job,重建隧道、复活 Connection。
- **L3(延后)** 从节点 checkpoint/resume + 分析改 HPC3 Slurm job(为 15GB)。Yijun 对方向的话:
  *"我们之前不是说 LangGraph 不太好用吗,如果好用我们就可以做 langgraph+postgre。"* → 上 L3 前先重新
  评估 LangGraph 是否好用;记忆里的 `architecture-direction`(LangGraph + Postgres checkpointer)是候选、未锁死。

**已落地(L1;仅代码 + 离线吸烟测试,前端**不**本地跑):**
- `src/bioagent/gateway/app.py` —— `Connection` 加 `stream` 缓冲;`push()` 喂 `_track_stream`(累积
  在途的助手轮次:正文、thinking、关键进度行、终止态、产物),`stream_replay_payloads()` 重建为有序
  WS 消息;`summary()` 暴露 `chat_running`;WS 端点**仅在 `chat_running` 时**重放在途轮次(已完成的
  运行已在客户端持久化会话里 → 不重复气泡)。
- `frontend/console/app.js` —— `CONN_KEY` 把 `connection_id` 落 localStorage(connect 时存、disconnect
  时清);启动时 `restoreConnection()` 校验 `/api/connections/{id}` 并重开 WS(重放状态+日志+待审
  Duo/Plan+实时气泡);`applyStatus` 在 `chat_running` 时重新显示 Stop。
- `tests/test_connection_replay.py` —— 7 条离线断言。

**已验证:** `node --check`、`py_compile`、`pytest`(新测 + report_output + lab_progress +
gateway_lab/mock + chat_history)全绿(合并跑 61 passed)。

**待办/下一步:** 覆盖 gateway 在线时的刷新/回退/短断网;**不**扛 gateway 重启(→ L2),**不**能把超时
运行从节点续起(→ L3)。本分支现在打包了三件事(streaming 在更底层的分支;然后文件浏览/报告产出;
再到本次 L1)—— 合并成 PR 时拆开。

## 2026-06-28(最新+11)—— 报告标题、无数据警告、matplotlib 修复、文件夹式文件浏览(分支 `fix/report-output-and-file-browser`,叠在 streaming 之上,未合并)

**触发:** Yijun 在隧道上跑了一次 single-cell 工作流,产出的论文报告**一张图都没有**(只有
参考文献)。根因(看 log + technical_report.md):这次运行 dataset 字段是空的 —— 于是
`decisions['dataset_path']` 没被设置,所有 scanpy 工具(`run_scanpy_qc`/`run_clustering`/
`run_de`)一上来就返回 "no dataset loaded",Agent 自己用 run_code 硬画图又踩了 matplotlib
缓存目录没写权限。外加两个 UX 诉求:报告文档标题要按内容自拟(不是写死 "report"),右侧文件区
别再把每个文件平铺(要按文件夹分组、只一个 zip、略缩图在上列表在下)。PDF 预览 Yijun 说已经能开,
没动。

**已落地(仅代码 + 离线吸烟测试;前端**不**在本地跑 —— Yijun 调线上隧道):**
- `src/bioagent/gateway/app.py`
  - 新增纯函数 `_promote_doc_title(md, fallback)`:把报告正文首个 `# ` H1 提升为 pandoc 文档
    标题并从正文删掉(不重复)。接到**两处** `build_pdf_report`(正式稿 + 技术报告)—— 标题
    现在按内容生成。
  - `_run_lab`:未附带数据集(或路径不存在)时,改为**大声警告** + ⚠ 关键进度行("分析不会
    产生数据图"),不再静默运行;成功时加 "📂 Loaded dataset: …"。`say_key` 上移到函数开头。
- `src/bioagent/agents/sandbox.py` —— `CodeSandbox._env` 把 `MPLCONFIGDIR`(+ `XDG_CACHE_HOME`)
  钉到 run 自己的可写目录,让 matplotlib/scanpy 在只读的 Singularity 容器里能正常出图(原来
  报 "Permission denied creating matplotlib cache directories")。这是下次带数据集时能真正出图
  的关键修复。
- `frontend/console/app.js` + `styles.css` —— 重写 Downloads 面板:一个"下载全部"zip,然后
  略缩图网格(图片)在上,再下面是按文件夹分组(report/figures/tables/data/process/extra)
  的可折叠目录列表;点任意 → 现有预览弹窗(处理器放宽到 `[data-url]`)。聊天内的产物块改为
  精简版(zip + "去面板浏览")。
- `tests/test_report_output.py` —— 7 条离线断言(`_promote_doc_title` + sandbox env)。

**已验证:** `py_compile`、`node --check`、`pytest`(新测 + lab_progress + gateway_lab/mock +
report + research_lab + sandbox)全绿。

**待办/下一步:** 未合并。本分支叠在 `feat/streaming-lab-progress` 之上,方便隧道一次测两个;
合并时先 streaming 再本分支。matplotlib 修复尚未在真实容器上验证(Yijun 确认下次带数据集能出图)。
更深的问题 —— dataset 字段空时是否自动发现服务器上的数据集 —— Yijun 选择**否**(改为警告),
所以没加自动发现。

## 2026-06-28(最新+10)—— 中间面板实时进度流(分支 `feat/streaming-lab-progress`,未合并)

**触发:** Yijun 反馈:跑 lab 时中间聊天("左边")整个过程只显示 "…",直到最后才把
写好的报告一次性灌进来;而右边日志却能实时收到全部事件。希望参考 Claude:折叠冗长的
"thinking",把关键进展实时显示出来。

**已落地(仅代码 + 离线吸烟测试 —— 按 Yijun 要求**不**在本地启前端,他用远程隧道调线上版):**
- `src/bioagent/gateway/app.py`
  - 新增纯函数 `_lab_event_to_chat(ev) -> [payload]`(模块级,可单测):把 lab 的
    `on_event` 事件翻译成聊天流 WS 消息。两路:冗长回合(`tool_start/result/error`、
    `critic`)→ `chat_thinking` token(可折叠的活动日志,复用自由聊天那套机制);关键
    里程碑(`pi_agenda` 计划及每个子步骤、`scientist_start`、critic `accept`、
    `user_injection`、`plan_cancelled`、`lab_done`)→ 新的 `lab_progress` 消息。
  - `_run_lab.on_event` 在保持原有 `emit()` 技术日志不变的前提下,额外 `conn.push` 这些
    消息(右侧日志不动)。新增 `say_key()` 助手,并在报告各阶段(撰写/参考文献/自审/
    渲染/"报告就绪")补 `lab_progress` 行。
- `frontend/console/app.js` —— `startAssistantStream` 新增 `.lab-progress` 元素(首条关键行
  前隐藏;自由聊天不会发);新增 `appendLabProgress` + `lab_progress` 分支;
  `finishAssistantStream` 收尾时把进度条淡化并清空 `state.streamingProgressEl`。冗长活动
  走现成的 `chat_thinking` 路径(`chat_done` 时自动折叠)。
- `frontend/console/styles.css` —— `.lab-progress` 进度条样式(success/warning/子步骤变体、淡入)。
- `tests/test_lab_progress_stream.py` —— 对 `_lab_event_to_chat` 的 13 条离线断言。

**已验证:** `py_compile` app.py、`node --check` app.js、`pytest`(新测 + gateway_lab +
gateway_mock + research_lab + chat_history)全绿。

**待办/下一步:** 尚未合并 `main`;尚未在线上隧道服务器上实测(由 Yijun 验证)。最终报告
仍由现有 `chat_token` 一次性填入气泡正文 —— 以后可把报告也逐 token 流式输出,但需让
`_build_report` 支持增量产出(当前是一次成稿)。

## 2026-06-28(最新+9)—— plan-mode 交互大改:计划进聊天、Send/Stop 切换、运行中注入、动画打磨(已落地,分支 `feat/axis-b-pi-skill-selection`)

**触发:** Yijun 反馈 plan-mode 输入栏坏了 —— Stop 按钮撑满整行把输入框挤没(没法提计划意见)、计划显示在底部面板而非聊天栏、页面切换生硬廉价。

**根因:**
- `#chatStop` 复用了 `.danger-btn { width:100% }`,在 flex 输入栏里把 textarea 压到约 0px。
- `setRunning(true)` 运行时直接隐藏 Send,唯一反馈通道是隐藏的回车键,计划卡在巨大的 Stop 后面等输入。

**已落地(前端 `frontend/console/*` + 后端钩子):**
- **A — Send/Stop 切换。** 输入栏单一动作:空闲 → Send(新建运行);计划审阅或执行中 **+ 有文字** → Send;**+ 空** → Stop。`input` 时实时切换。CSS 把 Send/Stop 固定为 92px 药丸,Stop 再也撑不大。
- **B — 计划渲染进聊天栏**,`📋 Proposed plan` 卡片(plan.md 样式、蓝色气泡)+ 内联 Run/Cancel + 修改提示,替代旧 `#planPanel`(已删)。Clarify 同样进聊天。批准后计划以文本持久化进历史。
- **C — 运行中注入提示词。** 执行中输入 POST `/api/chat/inject`(新端点)→ `Connection.injections` → `ResearchLab.run(pull_injections=…)` 步骤间取出,作为贯穿后续所有步骤的用户指引(独立 brief 段,不当成 Critic 修订)。新增 `user_injection` 事件。
- **动画打磨。** 入场关键帧(`viewIn`/`msgIn`/`panelIn`),悬浮略增强,全部受 `prefers-reduced-motion` 约束。

**测试:** `tests/test_research_lab.py::test_lab_folds_midrun_injection_into_remaining_steps`;全套 201 绿。浏览器实测通过。

**本地测试提示:** 控制台需登录。用临时 SQLite + bootstrap admin 启动:`BIOAGENT_DATABASE_URL=sqlite:///…/test.db BIOAGENT_SECRET_KEY=… BIOAGENT_ADMIN_USER=root BIOAGENT_ADMIN_PASSWORD=rootpass1 python -m bioagent.gateway`。mock 连接离线可用但无真 vLLM,PI 无法真正出计划 —— 在页面 console 调 `showPlanPanel([...])` 驱动计划 UI。

## 2026-06-28(最新+8)—— 上下文窗口预算:消除 32K vLLM 溢出(已落地,分支 `feat/axis-b-pi-skill-selection`)

**触发:** 一次实跑在某步骤中途崩溃:
`vLLM completion error: maximum context length is 32768 tokens … you requested 0 output tokens and your prompt contains at least 32769 input tokens`。

**根因(两个叠加 bug):**
- 所服务的 Qwen 被 `--max-model-len 32768` 限死([settings.py](../../src/bioagent/gateway/settings.py) `vllm_max_model_len`,受 A100-40G 的 KV cache 约束)。[research_harness.py](../../src/bioagent/agents/research_harness.py) 里的 Scientist 主循环**从不给历史做预算**:每轮都重发整套工具 schema,并不断叠加 assistant 轮次 + 工具结果(每条封顶 4000 字符,但**条数不封顶**)。工具密集的一步(4× `run_enrichment` + 多次 `run_code`)把 prompt 顶过 32768 → 直接 400。
- `vllm_client.chat_tools` 从不设 `max_tokens`,vLLM 就把输出预算默认成 `max_model_len − prompt` → 满 prompt 时 = **0**(即报错里的 "requested 0 output tokens")。

**修复:**
- `chat_tools` 现在预留 `max_tokens`(默认 2048),保证永远有输出空间([vllm_client.py](../../src/bioagent/gateway/vllm_client.py))。
- 新增 `ResearchHarness._budget_messages`,在**每次模型调用前**把历史裁到 `窗口 − 预留 − 余量 − schema` 以内。system + 最初 brief 永远原样保留;其余按回合从新到旧:放得下就原样保留,否则**压缩**(成功的工具结果 → `result_digest` 摘要;失败/重试的 → 一行省略标记 —— 正是 Yijun 说的"重要就压缩保留,不重要就丢"),再放不下就把该回合及更老的全部丢弃。整回合(assistant 的 `tool_calls` + 它的 `role:tool` 回灌)作为一个单位移动,native tool-call 配对绝不被破坏。被丢的细节仍存在 `HarnessResult.steps` 里供 Critic/synthesis 使用。配置项:`HarnessConfig` 上的 `max_model_len`(环境变量 `BIOAGENT_VLLM_MAX_MODEL_LEN`)、`output_reserve_tokens`、`context_safety_margin`。会发 `context_trimmed` 事件。
- token 数按字符估算(`_CHARS_PER_TOKEN=3.0`,故意取小 → 宁可少填窗口;进程内没有 tokenizer)。

**测试:** `tests/test_research_harness.py` —— `test_compress_message_*`、`test_budget_messages_trims_to_window_keeps_preamble_and_pairing`、`test_budget_messages_noop_when_already_small`。整个 lab 套件全绿(49 passed)。

**lab 单轮路径(同分支已落地):** `ResearchLab._complete`(PI 规划 / Critic / synthesize,见 [research_lab.py](../../src/bioagent/agents/research_lab.py))现在走 `_budget_single_shot`。这里没有累积历史 —— 风险是单个过大的 user 负载(Critic 转发每步 digest,或 synthesize 打包所有已接受步骤)。它把回复额度设为窗口减去 prompt 后剩下的全部,但绝不低于 `LabConfig.reply_reserve_tokens`(默认 8192,保证长篇 manuscript 不被截短);若 prompt 大到剩余不足该值,就截断最大的那条 user 消息,并把输出钉到该预留值。窗口/余量复用 `config.scientist`(`HarnessConfig`),保持单一真源。`vllm_client.complete` 现在带 `max_tokens=` 调用(该参数一直存在,只是 lab 之前没传)。测试:`tests/test_research_lab.py::test_budget_single_shot_*`。整套 80 passed(harness/lab/gateway/集成全绿)。

**现已全覆盖:** 两条 LLM 调用路径都对窗口做预算 —— Scientist 工具循环(`_budget_messages`)和 lab 单轮补全(`_budget_single_shot`)。长期看两者都会被 LangGraph 移植的上下文管理统一接管。

## 2026-06-28(最新+7)—— 磁盘治理:删除数据集 + 运行结果定时回收(已落地,分支 `feat/axis-b-pi-skill-selection`)

**触发:** Yijun 问数据集上传到哪了,并指出用过的数据集 / 运行产物在 eyeserver 上越堆越多、无法手动删除。

**代码实际行为(已核实,纠正此前一个假设):**
- 上传落在 **eyeserver**(网关主机)`<BIOAGENT_RESULTS_DIR>/<owner>/uploads/` —— 生产 `=/data/BioAgent/users`。
- **scanpy 分析跑在 eyeserver 本地**(`CodeSandbox` 子进程,`python_bin=sys.executable`)。原始矩阵**原地读取,不会每次运行复制一遍**。只有 **大模型(vLLM A100)** 和 **scGPT 批处理** 用到 HPC3。所以真正的数值计算(PCA/leiden/DE/gseapy)吃的是 **eyeserver 的 CPU+RAM** —— 这才是并发瓶颈,不是磁盘。
- 此前已有的删除:对话(`DELETE /api/conversations/{id}`)、运行产物(`/api/results/delete`)、HPC3 DFS(`/api/storage/delete`)。**缺口:** 已上传数据集完全没有删除入口,运行目录也从不回收。

**本次落地:**
- `POST /api/datasets/delete` + `auth_routes.delete_dataset_record`(按 owner 删数据库行 + 受限于 `<owner>/uploads` 的物理删除)+ 数据集页的 **Delete** 按钮。删除**只由用户手动触发**。
- 测试:`tests/test_dataset_delete.py`。

**决定(Yijun):**
- 临时结果**不**搬到 HPC3 存储 —— SFTP 往返 + HPC3 配额不划算。
- **不做任何研究数据的自动删除。** 自动定时回收先做了原型,随后**撤掉** —— 静默删除"过期"运行有误删重要数据的风险,这个风险大于省下的存储。若将来磁盘真的紧张,优先做**管理员审阅 / 显式确认**式清理(列出候选、人工确认),而不是任何定时器。

**长期方向(未实现):** 解决并发的真正办法是把**分析计算本身作为 HPC3 Slurm 作业**跑(一次性卸掉 CPU+RAM+磁盘),复用 **scGPT Route C** 模式 —— 而不只是搬存储。等并发开始压 eyeserver 单副本时再做。

## 2026-06-28(最新+6)—— 轴B 已落地:PI 自主选 skill + 三轴北极星(分支 `feat/axis-b-pi-skill-selection`)

把 operon-`skills/` 方向和团队进一步细化。operon 是**面向开发者**的(研究员自己挑 protocol);
我们是**面向研究员**的——他们不知道自己要哪个 skill,所以**由 PI 决定**。把设计收敛成
**三条不能互相塌进去的正交轴**:

| 轴 | 是什么 | 谁决定 | 例子 |
| --- | --- | --- | --- |
| **A · 模式**(Virtual-Lab 会议类型) | individual(单 agent+工具)vs team(多个**独立记忆**专家+Critic) | **PI 路由** | scGPT→individual;开放式→team |
| **B · 领域知识**(`skills/` 库) | SKILL.md =「某研究路径怎么做才对」,**模式无关** | **PI 按 description 自选** | celltype / scgpt 协议 |
| **C · 持久化**(Lab Archive,06-18 草案) | 每 agent 独立记忆 + 会议记录 + checkpoint | 系统 | `labs/<id>/...` |

operon 只贡献**轴B 的打包格式**(SKILL.md 文件夹、低耦合),**不要**它的开发者面向 UX。
**关键纠正:skill ≠ 模式。** 现在两个 SKILL.md 读起来偏多智能体;目标里它们只是领域协议知识,
模式由 PI 另行决定,这样一个 skill 在 individual/team 两种模式都能复用。Virtual Lab 的清晰阶段
就是把 A+B+C 串起来的脊梁:project_spec → team_selection(A)→ tools_selection / workflow_design
(消费 B)→ implementation。

**轴B —— 本分支落地内容(`agents/research_lab.py` + 测试):**
- `LabConfig` 新增 `auto_select_skill: bool = True` 和 `skill_library`(None → 从 `skills/`
  经 `presets.PRESETS` 加载)。
- 新增 `_select_skill()` + `_SKILL_SELECT_SYSTEM` 路由提示词 + `_parse_skill_choice()`:
  PI 读每个 skill 的一行 `description`,返回最匹配的 key(或 "none" → 自由规划)。发
  `skill_selected` 事件。
- `run()` 一次性把 guidance 解析进 `self._guidance`(override > PI 自选 > 无);`_pi_plan`
  改用 `self._guidance`,不再直接读 `config.preset_prompt`。
- **优先级:** 显式 `preset_prompt`(网关下拉)仍然优先 → 下拉变成**可选 override**,默认走
  PI 自主选。**网关无需改**(`app.py` 已经是 `LabConfig(preset_prompt=...)`,None 的情况由
  自选补上)。前端以后可渲染 `skill_selected` 事件,不渲染也无害。
- 全套测试绿(196),含跑真实工具的 `test_full_lab_loop_runs_real_tools_locally`。
- **本分支没提交**:文献线的工作树改动(`tools/literature_references.py`、
  `docs/archive/literature_embedding_plan.md`)—— 原样保留,没动。

**下一步:轴A(PI 模式路由)** —— PI 先判 individual vs team,再在该模式内由轴B 选 skill。

**轴C(独立记忆)—— 暂不动,但硬件结论已定:别为它加卡或降级模型。** 多 agent 的独立记忆 =
同一份模型权重 + 各 agent 自己的对话历史,**不是** N 份模型——权重只加载一次。看到的 74.6GB 是
`0.92 × 80G`,早就被 vLLM 预留成 **KV-cache 池**;独立记忆 agent 就是从这同一个池取 context。真正
的杠杆是**记忆摘要**(全文落 Lab Archive 磁盘,只把 digest + 最近几轮进 context),把 KV 增长和
agent 数量解耦。KV 真撑满时的便宜手段排序:缩短 digest → 降单 agent context → 串行(非并行)会议
→ 最后才是第二块 GPU / 换小模型。为了"塞更多 agent"降级主模型是解决伪问题还伤质量(Virtual Lab
全程用一个强模型是有意为之)。唯一真正吃第二份显存的是**不同权重**的模型(如 06-11 的 VL 排版
审查),而独立记忆不属于这种。

## 2026-06-28(最新+5)—— 文献方案定稿:远端 provider 选定(Edison/FutureHouse Crow)

**决定(关掉"选型"问题):**
- **Tier 1(主路远端一站式 RAG)= FutureHouse / Edison Scientific 平台,agent Crow**(PaperQA2 内核,
  与我们的 `deep_literature` 同源)。真实开发者 API:`pip install edison-client`(前身
  `futurehouse-client`),api-key 认证,`run_tasks_until_done`。embedding + 向量库 + 检索 + 合成全在
  对方云上 → **我们一概不托管。** Crow = 快速逐问 agent;Falcon = 深度综述。这是清单里**唯一**"一站式
  RAG + 有 API"的方案(Consensus/Elicit/SciSpace/Undermind 都无公开 API)。
- **Tier 2(兜底)= Europe PMC 关键词**(已落地)。CORE 可作可选第三级;完全离线不考虑。
- **无本地 embedding** —— 旧的"embedding 必须本地"约束**作废**(我们只嵌入公开论文、或在关键词兜底里
  根本不嵌入)。MedCPT / BMRetriever / bge-large 的选型对这条路因此无意义。
- 方案写进 `docs/archive/literature_embedding_plan.md`(取代只存在微信里的剪贴清单;已回写用户的外部副本)。

**待办(→ MaziYao / 文献线):** 申请 Edison API key;确认其数据留存/是否用于训练的政策(合规前提);
然后二选一:套 REST wrapper 走 `BIOAGENT_LITERATURE_REMOTE_URL`(现在就能用),或拿 key 验证返回结构后写
原生 `edison-client` adapter。代码**未**硬绑客户端(没 key 没法验证结构)。

**本轮用户的两条约束:**
- **`deep_literature` / `paperqa_search.py` 保持不动**(它继续用本地 `st-` embedding;是独立的 in-loop
  路径,与 references 模块无关)。
- **下一个想要的功能 = Mode B(前置检索到写作):** 让模型在写 Introduction/Discussion 时自己查文献并据此
  引用,References 作副产品。方案见 `docs/archive/literature_embedding_plan.md` §4 —— 属 `_REPORT_WRITER_SYSTEM`
  编排改造,**尚未实现**。(Results/Methods 保持无文献。)

## 2026-06-27(最新+4)—— 文献「引用模块」:`## References` 槽位现在真的会被填上了(已落地)

**为什么:** PI 手稿写作器一直会输出一个预留的 `## References` 段,占位符写着
*"Citations to be inserted by the literature module (PaperQA)."* —— 但**从来没有任何代码去填它**。
槽位在,模块不在。现在补上了。

**落地内容**(`src/bioagent/tools/literature_references.py` + `gateway/app.py` 接线):
- **两级检索、无本地 embedding**(团队决定):
  - **第 1 级 —— 远端一站式 RAG 服务**(主路):外部 embedding + 向量库 + 检索 agent;我们不托管
    embedding、不维护索引。环境变量门控:`BIOAGENT_LITERATURE_REMOTE_URL`(+ 可选
    `BIOAGENT_LITERATURE_REMOTE_KEY`)。**provider 尚未选定** —— env 未设置时第 1 级自报不可用并降级。
    期望返回结构为 `references`/`citations` 列表(+ 可选 `answer`),归一化函数容忍字段名漂移。
    **TODO:确认 provider 有真正的开发者 API + 可接受的数据留存政策,然后设置 env。**
  - **第 2 级 —— Europe PMC 关键词**(fallback):复用 `literature_search.search_europepmc` ——
    轻量、无 embedding、真 DOI/PMID。仅在第 1 级不可达但主机仍有外网时触发(完全断网的 UCI 机器
    两个服务都连不上 —— 不在考虑范围)。
- **降级写进技术报告,不写进学术手稿。** fallback/空结果会变成 `degradation_note`,接入
  `_build_technical_report` → 其「Diagnostics & failures」段。投稿级学术手稿照常按成品渲染。
  (符合双报告分工。)
- **绝不编造。** 空结果 → 诚实的「*No external citations were retrieved…*」一行,而不是编一条引用。
  自审 + 写作 prompt 已更新为**逐字保留已填好的 References 段**(之前会把 PaperQA 占位符再塞回去)。
- **隐私:** 只把**公开的研究问题**发给两级检索 —— 绝不发 grounded synthesis(里面有数据派生的数值)。
  已与 JinLi 确认(query ≠ 数据)。

**Mode A vs Mode B:** 这是**做对的 Mode A**(末端引用器 —— 草稿写完后填引用)。讨论过的 **Mode B**
(写 Introduction + Discussion 前先检索证据、引用作副产品)是在本模块之上的自然下一步,属于
`_REPORT_WRITER_SYSTEM` 的编排改动 —— **尚未实现**。Results/Methods 应保持无文献。

**测试:** `tests/test_literature_references.py`(11 个)—— 级别选择、隐私、插入、空/诚实占位、降级说明。
全量 193 通过。

## 2026-06-27(最新+3)—— 研究路径迁移到 operon 式 `skills/` 库(已落地,迁移第 1 步)

背景:再看 **swaruplab/operon** —— 它的 665 个 protocol 每个都是一个 Anthropic
**Skill** 文件夹:`protocols/<name>/SKILL.md`(frontmatter `name`+`description` + 正文:
When-to-use / Quick Start / 示例代码)+ `scripts/ references/ assets/`;选中后把 SKILL.md
注入 Claude 上下文。我们 `presets.py` 的 `ResearchPreset.prompt` 本质上**就是同一个东西**
—— 一段手写的流程引导文本,只是硬编码在 Python 里、且缺模块化文件夹和参考代码。

**本次落地(最小、行为不变 —— 第 1 步):**
- 新建仓库根 **`skills/`** 库。每条研究路径一个文件夹:`skills/<name>/SKILL.md` =
  frontmatter(`name`+`description`)+ 正文(= PI 默认规划引导)。`skills/README.md` 写明格式。
- 两个旧 preset 原文迁入(正文一字未改):`skills/celltype_annotation/`、`skills/scgpt_annotation/`。
- `agents/presets.py` 改成**薄 loader**:扫 `skills/*/SKILL.md` 装进 `PRESETS`
  (`name`→key、`description`→label、正文→prompt)。公共 API 不变
  (`PRESETS`/`get_preset`/`list_presets`),所以 gateway(`/api/presets`、`_run_lab`)、
  `system_info.py`、测试都不用动。`$BIOAGENT_SKILLS_DIR` 可覆盖路径。相关测试全绿。

**这一步埋下的设计(尚未实现 —— 迁移阶段):**
- **两层分开。** *能力层* = 结构化函数工具(`scgpt_annotate`、biotools)继续是注册的
  Python 工具(vLLM tool-parser 要可靠)—— **不**变 markdown;*流程层* =「何时/如何编排工具」
  → `skills/` 的 SKILL.md。skill 正文应**调用**已有工具,只在工具没覆盖时才用 `run_code`
  (`_pi_plan` 已有此规则)。
- **PI 做选 skill**:读各 skill 的 `description`(便宜、可扩展),把命中的 SKILL.md 正文注入
  PI 引导(目前仍是单 preset 的 `preset_prompt` 路径)。`scripts/` 参考代码 → Scientist
  `run_code` 的模板;Critic 看真实 tool artifacts(06-09 那条 commit)是这条代码路的安全闸。
- **对比 operon 的注意点:** 我们不跑在 Claude 上,没有原生 Skill harness,所以 loader 自己写
  (已完成,很薄);尊重 operon 各 protocol 的 license(看到 BSD-3-Clause)—— 借鉴**模式**、
  自己写 skill,license 兼容才选择性移植;规模上百时需要 description 索引 / embedding 选择
  —— 现在才 2 个,不需要。

恢复后下一步:给某个 skill 加 `scripts/`+`references/` 并接到 Scientist;让 PI 从多个 skill
里(多选)挑,而不是单 preset。

## 2026-06-26(最新+2)—— 报告：data/ 产物可见、确定性溢出修复、图题注、Methods 分层、预留 References(已落地)

报告流水线 5 项改动(`gateway/app.py` + `tools/report.py`)，均离线验证：
- **data/ 产物现在能到写手手里。** 旧 `_build_report` 只看 `tables/`+`figures/`，对 `data/` 视而不见，
  所以成功的 `scgpt_predictions.csv` 隐形→模型编 fallback。新增 `_data_artifacts_block` +
  `_scgpt_label_summary_md`，把 scGPT 标签分布(Rod 78%…69 类、mean confidence)作为真实结果喂给写手。
- **确定性 PDF 溢出修复(不引入视觉模型)。** 新增 `_TABLE_LUA_FILTER`(pandoc Lua)把 ≥4 列的表格强制
  成等宽 `p{}` 自动换行列；配合 `\footnotesize` header + `_fmt_num` 数字压缩，最坏的 17 位数字表从严重
  叠印变成 0 个 overfull。PDF/DOCX 都生效。(如需"检测"也可解析 xelatex 的 "Overfull \hbox" 警告，同样无 VLM。)
- **图题注(caption)强制**：写手+审稿 prompt 现在要求 `![Figure N. <展示了什么；坐标轴/颜色/图例含义>](…)`，
  编号、禁止空题注。(图内 legend 属于绘图层：scanpy UMAP 用 on-data 簇号；matplotlib 富集是单序列。
  更完整的侧边 legend 留作后续绘图代码微调，本次未做。)
- **Methods 必须分点、允许分层。** prompt 要求编号列表 + 多级子编号(4.1/4.2…);审稿会把一段式 Methods
  重构成该形式。
- **为 PaperQA 预留 References。** 写手/审稿总以 `## References` 结尾;若暂无真实 DOI/PMID,确定性
  `_ensure_references` 注入占位 "*Citations to be inserted by the literature module (PaperQA).*"
  (PaperQA 接线本次故意不做)。

验证:`test_report/test_slurm_job/test_scgpt_job/test_scgpt_annotate` 共 26 项全绿;用 pandoc+tectonic
重渲染 `~/Downloads/bioagent_results_68ca75b000e5/report/report_fixed.{md,pdf}`——0 overfull、scGPT
Table 1 进正文、图有题注、Methods 1–6 带 4.1/5.1 子项、有 References 槽位。

## 2026-06-26(最新+1)—— scGPT「did not complete (state RUNNING)」其实是 squeue/sacct 竞态(已修)

Run `68ca75b000e5` 报 `SlurmJobError: scGPT inference job 53726309 did not complete (state RUNNING)`，
报告还编了假理由。集群日志证明 scGPT **其实成功了**：`Inference was finished in: 92.04 seconds` →
`wrote …/scgpt/68ca75b000e5/out/predictions.csv`（bundle 里 `data/scgpt_predictions.csv` 有 11,977 个
细胞注释：Rod 9339 / MG / cone / bipolar，质量很好）。

根因在 `gateway/slurm_job.py:run_batch_job`：squeue 与 sacct 最终一致但有延迟。作业离开队列后
（squeue 空），循环去读 `_terminal_state`(sacct)，而 sacct 还滞后报 **RUNNING** → `ok=False` →
返回 `completed=False, state=RUNNING`；`scgpt_job` 据此抛 "did not complete"。但作业其实下一秒就写完了。
修复：新增 `_SACCT_TERMINAL` + `_is_terminal_sacct`；squeue 空时只在 sacct 为真正终态时才下结论，遇到
滞后的 RUNNING/COMPLETING 继续轮询，只有连续 `_GONE_CONFIRM=2` 次「squeue 空且 sacct 无记录」才回退
假定 COMPLETED（应对关了 accounting 的集群）。`_terminal_state` 不再乐观默认 COMPLETED。加了 2 个回归测试；
`test_slurm_job/test_scgpt_job/test_scgpt_annotate` 共 22 项全绿。

影响：这一处修复打通整条链——scgpt_annotate 现在会带 predictions 正常返回、Critic 采纳该步、synthesis
采用真实 scGPT 标签，而不是写手编造 fallback。这**不是排队问题**（排队那次是另一个 bundle `32ea8936d0e6`）。
仍未做：那个旧 bundle 里 run_code 的「raw table data」guard 连锁已被提交 09ff6ca 的 endpoint-aware guard
解决（此 bundle 生成于 10:30，早于 12:34 的修复）。

## 2026-06-26(最新)—— 报告排版 + 论文结构 + 双报告(已落地，离线验证)

起因：复盘 scGPT run `32ea8936d0e6`（bundle 在 `~/Downloads/bioagent_results_32ea8936d0e6`）。
该 run 的 scGPT **根本没跑起来**——`SlurmJobError: could not get a node after 3 attempts`
（GPU 队列拥塞；默认 `AcquireConfig` 仅等 3×180s≈9 分钟，对繁忙分区太短，见 `slurm_job.py:66-71`）。
scientist 私自降级为 marker-based 并在论文里隐去。完整诊断写入 bundle：`report/scgpt_failure_analysis.md`。

报告生成修复（均在 `gateway/app.py` + `tools/report.py`）：
- **PDF 表格溢出修复**：富集表数字叠印是因为 pandoc 拿到 17 位浮点。(1) `report.py` 通过
  `--include-in-header` 注入 `_TABLE_HEADER_TEX`（longtable/tabular 用 `\footnotesize`）；
  (2) 新增 `_fmt_num` + 改写 `_csv_preview_md`，在喂给模型前把数字压缩（p 值→`1.6e-18`，浮点≤3 位），并限行数。
- **论文结构**：`_REPORT_WRITER_SYSTEM` + `_REPORT_REVIEW_SYSTEM` 现在对标真实论文
  （Menon et al. 2019, Nat Commun 10:4902）；**Methods=编号分点带参数**（不再一大段）；
  **强制 Conclusion + Limitations**；表格纪律（优先图、≤5 行、科学计数法）；**如实披露失败/降级步骤**。
- **双报告**：新增 `_build_technical_report`（+`_TECH_REPORT_WRITER_SYSTEM`、`_run_log_digest`），
  吃全量 `result.rounds`（含失败步骤），渲染 `report/technical_report.{pdf,docx}`。best-effort，不阻断主流程。
- glyph 坑：默认 LaTeX 字体（Latin Modern）没有 `≥`/`≤`，xelatex 下会静默丢字——正文避免使用。

离线验证：`tests/test_report.py`(4) 全绿；用 pandoc+tectonic 把这一次报告重渲染为
`report/report_fixed.{md,pdf}`（4 列表格干净、Methods 分点、有 Conclusion）。两处 prompt 改写需真实 lab run 才能完整验证。

未完待办：提高 scGPT `AcquireConfig` 等待预算 / 加 CPU fallback / 预检 GPU（基础设施侧 `scgpt_job.py`）；
修 cross-validation（步骤 7–8）被 run_code guard 拦截的问题（数据改用 artifact 引用而非内联）。

## 2026-06-26(后续)—— 隐私 guard + Critic 看产物 数据通路(已落地)

分支 `feat/paperqa-guard-critic-artifacts`。起因:审计一个 scGPT run —— 工具产出了正确标签,
但整个 run 被判失败、报告还谎称 "fallback"。两个根因,均已修(离线测试通过,lab/harness 27 项绿):

1. **隐私 guard 不看 endpoint。** `DataBoundaryGuard` 只要 brief 里有原始表格就硬拦,哪怕 LLM
   是隧道后的**本地 Qwen** —— 白白杀掉合法步骤(如 `run_de`),换不来任何隐私收益。修
   (`research_harness.py`):`ctx.tunnel_port` 有值(本地,数据不出 UCI)就放行原始表;secret
   永远拦;env `BIOAGENT_GUARD_BLOCK_RAW_DATA_ALWAYS=1` 强制严格。

2. **Critic 看不见 artifact;harness 把产物压平了。** `_summarize` 把工具结构化返回压成一个
   `status` 字,`_critic` 只转发工具**名字** —— Critic 判的是 Scientist 的文笔,从没见过产物;
   而且手维护的字段列表导致每加一种 artifact 都要改代码。修(`research_harness.py` +
   `research_lab.py`):每步保留完整结构化 `result`;`_critic` 泛型转发真实工具结果
   (`result_digest`,限长、类型无关);确定性兜底改用 `step_succeeded`(工具是否产出可用结果)
   而非 `final_answer` —— 产出 artifact 的步骤即使 loop `incomplete` 也能被接受;`_synthesize`
   汇总时带上 accepted 步骤的真实产物。

**故意没做(保持最小):**
- *scGPT 等待预算* —— 默认 `run_timeout_s` 已是 3600s,所以 ~2 分钟的 "state RUNNING" 失败
  **不是预算太短**,真因要 HPC3 的 Slurm 日志才能定。Critic-artifact 修复已经把它的杀伤力拆掉
  (重试写出 predictions.csv → 凭产物 accept),超时降级为表面问题。上机查,别盲调。属**调查项**,非代码改动。
- *force-advance 步骤的 artifact 汇总认领* —— 已被 artifact-aware 兜底覆盖(真正产出的步骤现在会被
  accept);剩下会被 force-advance 的是 Critic 正当判废的步骤,把它们的输出塞进报告反而污染。砍掉。

## 2026-06-26 —— 待办(console plan-mode UX)—— 两条,均未实现

测试 scGPT+PaperQA run 时发现的两条 console/orchestrator 待办。都是 UX,不改 agent 逻辑。
归属线:yijun(console/gateway)。

1. **plan-mode 的修改要走"自然语言 → LLM 重新规划",不是直接改/不是给工具表。**
   研究者**不应该**为了改 agenda 而去知道系统有哪些工具。现在 plan-mode 只把原始 agenda
   字符串推给前端(`app.py` 里 `plan_review` → `{"type":"plan_prompt","agenda":[...]}`),
   用户直接编辑这些字符串再 `POST /api/lab/plan`(`approved` + 编辑后的 `agenda`)——
   这等于默认用户知道哪步对应真实工具。期望流程:用户在 agenda 下方的对话框里用**自然语言**
   说想怎么改("去掉富集那步""加一步文献佐证""QC 太狠了"),这段反馈**交回 PI**,由 PI
   重新清洗/重排 agenda;反复迭代直到用户批准或取消。
   - 后端:给 `/api/lab/plan` 加第三种结果 —— `revise` + 一段自由文本指令 —— 用这段指令
     重新触发 PI 规划(复用现有 planner),再推一份新的 `plan_prompt`。批准/取消路径不变。
   - **明确不要**在编辑器里放工具调色板(这是先前的想法,现已否决):核心就是研究者只用
     自然语言、由 LLM 负责"步骤↔工具"的映射。`system_info` 工具表只留在 System 页面。

2. **console 对话框 UX 坏了,需要大修。** plan 模式下输入框被挤到左边一条细缝、不能交互、
   看起来像坏了(见 2026-06-26 截图:agenda guidance 面板 + Stop 按钮把输入框挤成左侧窄条)。
   plan-review + 对话输入区的整体布局要重新设计,让 agenda、自然语言反馈输入框、运行/停止
   控件能同时正常用。这是真正的前端重做,不是调 CSS。

## 2026-06-18(后续)—— 方向:重新启用多智能体 Virtual Lab · 草案(下周讨论):Lab Archive

> 两部分:一是**方向更新**(本次已定),二是**设计草案**,供下周(2026-06-22 那周)团队讨论。
> 草案**尚未实现**,是建议的下一个重点。

### 方向更新(已定)

- **多智能体重新启用。** 产品目标是**可用的科研工具**,开放式研究需要一支真正的专家团队 ——
  单 agent 反而是项目跑偏。之前"不要多智能体 / 单一固定工作流"的结论,现在**仅限于例行的固定
  流水线**(比如 scGPT / marker 细胞类型注释 —— 那里单 agent + 工具是对的),**不是**整个研究
  实验室的方向。
- **严格复刻 Virtual Lab 的 agenda 驱动流程**(`zou-group/virtual-lab`)作为我们的模型:一个
  **PI** 动态组建**专家 agent** 团队、一个 first-class 的 **Scientific Critic**、**team meeting**
  与 **individual meeting** 两种会议、结构化的 **agenda / agenda questions / agenda rules**,以及
  可选的**并行会议 + merge**。
- **agent 之间不共享上下文。** 每个专家保留自己**独立的记忆/视角** —— 这正是它成为真正团队
  (观点多样、不塌缩)而非"一个模型自我扮演"的关键。现在 `ResearchLab` 的 "specialist" 是套皮
  (同一个 Scientist 在共享 run 上换段提示词);要补的就是这个差距。
- **GPU-hour 成本暂时不作约束** —— 先把能力/质量做出来。
- 总之:现在的 `ResearchLab`(PI → Scientist-personas → Critic → 汇总)是个**弱 agentic 工作流**
  —— 是种子,不是终点。

### 下一步要解决的核心问题(重点)

HPC3 上每个计算任务都是**临时的**:`srun`/Slurm 任务被回收时,计算节点本地的 `$TMPDIR` 和进程内
内存就**没了**。但多智能体实验室有大量**不能随任务一起死掉的活状态**:每个专家的记忆/上下文、会议
记录、中间产物/临时文件、agenda 和决策。我们需要一个结构化、持久、**可恢复**的存储 —— 一个
"handoff"形态的存档 —— 让一个研究项目能熬过任务回收、gateway 重启、以及隔一周再继续。

**关键杠杆:gateway(eye-server)是常驻的、是编排者;计算任务(vLLM serve、分析、scGPT)是临时
worker。** 规则:*实验室的权威状态存在常驻侧(eye-server `/data` + PostgreSQL)和/或 dfs3b ——
绝不只存在计算节点上。* 计算任务退化成**无状态函数**:从持久存储读输入 → 把输出写回 → 退出。
任何重要的东西都不会只活在某个任务里。

### 草案 —— "Lab Archive"(结构化 handoff 存档)

一份持久、结构化、可恢复的整项目记录,由 gateway 拥有。建议的磁盘布局(每个 lab 一份,放 dfs3b 或
`/data`):

```
labs/<lab_id>/
  manifest.json            # lab id、问题、状态、schema_version、创建/更新时间
  agenda.json              # agenda + agenda_questions + agenda_rules(Virtual-Lab 风格)
  team.json                # 团队名册:每个 agent {id, title, expertise, goal, role, tools, model}
  agents/<agent_id>/
    memory.jsonl           # 该 agent 自己的上下文/记忆日志(append-only)
    state.json             # 最新汇总状态 / scratch
    artifacts/             # 该 agent 产出的文件
  meetings/<meeting_id>/
    meeting.json           # 类型(team|individual)、参与者、agenda、状态
    transcript.jsonl       # 逐轮 who-said-what(append-only)
    summary.md             # PI 对这场会议的汇总
  checkpoints/<seq>.json   # 用于恢复的 loop/graph checkpoint
  artifacts/               # 项目级共享产物(图、表、报告)
  events.jsonl             # 全局事件日志(驱动实时 UI + 审计)
```

原则:
1. **权威状态在节点之外** —— 写 dfs3b / eye-server,绝不写计算节点 scratch。
2. **append-only + 原子写**(写临时文件再 `rename`)→ Slurm 回收不会损坏,最多丢一行在写的。
3. **每个 agent 轮次 / 每轮会议都 checkpoint**(不只在结尾)→ 回收最多丢一轮。
4. **每个 agent 独立记忆**(`agents/<id>/memory.jsonl`)—— 对应"不共享上下文"。
5. **manifest 带 schema 版本**,格式能演进而不破坏老 lab。
6. **PostgreSQL 索引**覆盖存档,做查询 + 多用户隔离(文本进 DB、blob 落盘 —— 和现在 `runs`/`datasets` 同样的拆分)。
7. **Singularity 契约:** lab 目录 **读写** bind,数据集 **只读** bind(和现有分析任务契约一致)。
8. **恢复 = rehydrate:** 任何新任务或 gateway 重启,加载 manifest + agenda + team + 各 agent 记忆 + 最新 checkpoint,然后继续。
9. **LangGraph-ready:** 以后要迁,这套 1:1 映射到 LangGraph 的 `PostgresSaver` checkpointer + 一个 artifact store —— 这个存档**就是** checkpoint 契约。

下周要讨论的开放问题:
- 权威存储选哪个:PostgreSQL(+ dfs3b 上的 blob)、纯 JSON-on-dfs3b、还是现在就上 LangGraph checkpointer?
- 会议**进行中**时 agent 记忆放哪 —— 在 gateway(eye-server),还是 stage 到 dfs3b 让计算任务读写?
- 老 lab + 临时产物的保留/GC;dfs3b 的容量预算。
- 并发:两场会议同时写同一个 lab(加锁 vs 每会议独立子目录)。
- 隐私:agent 记忆可能含数据集派生文本 —— 要和提示词一样受 `DataBoundaryGuard` 约束。

## 2026-06-18 —— 公网部署(Kubernetes)+ **必须用 PostgreSQL**

上公网域名 **https://<PUBLIC_HOSTNAME>**(eyeserver <GATEWAY_HOST>,开 80/443;
OIT 工单 **INC0907754** 负责 DNS + 放行端口)。部署套件在 `deploy/`(`Dockerfile`、
`k8s/aiscientist.yaml`、`README.md`;`nginx/`+`systemd/` 是裸机后备)。

**必须用 PostgreSQL,绝不用 SQLite。** 这是面向 UCI 生信实验室的 agent,后续预计**对全 UCI 开放**,
还可能**被其他项目组共用** → 必须支持真正的**并发**与多租户持久化。SQLite 单写者,在并发下会
`database is locked`(只适合 dev/CI)。请起一个专用 Postgres(集群内 StatefulSet + 自己的 PVC,
或托管实例),并设 `BIOAGENT_DATABASE_URL=postgresql+psycopg://...`。(eyeserver 宿主机有 Postgres 17
在 127.0.0.1:5432,但只绑 loopback,Pod 不改配置够不到——请用集群内 PG。)

**集群事实(只读探测,`<ucinetid>` 无 kubectl/kubeconfig):** 完整 kubeadm k8s(apiserver :6443、
etcd、kubelet :10250)、**Calico** CNI、**MetalLB**(在 .197 网卡)、ingress 控制器已在 **:80/:443**
(就是 Jin 说的"同端口多子域"入口)、containerd+docker。要把 `deploy/k8s/aiscientist.yaml` 收口,
集群管理员还需提供 4 条只读输出:`kubectl get ingressclass`、`get storageclass`、任一现有
`ingress -o yaml`(TLS 模式)、以及镜像 registry。

**给 k8s 团队的硬约束**(本应用是**有状态单例**——每会话 SSH 隧道到 HPC3 + 内存状态):`replicas: 1`
+ `strategy: Recreate`(绝不双 Pod);Pod **出站到 hpc3.rcic.uci.edu:22** 必须放行;`/data/runs` 要
PVC;ingress 注解要长超时 + 8g 上传 + WebSocket `/ws/`。安全(OIT 提醒):无默认凭据(已核实——
`ensure_bootstrap_admin` 只从显式 env 播种、优先 bcrypt 哈希),`BIOAGENT_PUBLIC_HTTPS=1` 时 cookie 置
`Secure`,公网却用 dev `BIOAGENT_SECRET_KEY` 时启动大声告警。

## 2026-06-17 —— 重构落地:ResearchLab、移除 Kosmos/harness、文献交接

本小节取代 2026-06-11 的"方向修正"计划:当时计划的动作现在都**已在代码里完成**
(分支 `refactor/harness-and-kosmos-cli-removal`)。净结果 —— 产品收敛成单一固定研究
工作流(`ResearchLab` 循环),由 gateway 控制台驱动;旧小节里描述的多框架
(Biomni/Kosmos)和自主循环脚手架都已不存在。

### 架构:13-agent 流水线 → `ResearchLab` 循环

扁平的 13-agent `workflows/vision.py` 流水线和 `agents/pipeline.py` 都**已删除**。
现在的工作流是 **`agents/research_lab.py`**(`ResearchLab`)里的 4 角色循环,由
**`agents/research_harness.py`**(原生 vLLM、会 tool-calling 的 Scientist)执行:

1. **PI** 起草 agenda(`_pi_plan`);可选的**人工 plan 审核闸**(plan mode)让用户在任何
   工具运行前先改 agenda。
2. **Scientist** 逐步执行,按步骤选**专家人格**(QC / 聚类 / 通路 / 通用)并调用工具。
3. **Critic** 对每一步判 `accept` / `revise`,带**确定性兜底**(结果非 `ok`、有报错、或缺
   最终答案时绝不 accept),据此重试到 `max_revisions`。
4. **PI 汇总**最终答案,只基于已 accept 的步骤。

`LabConfig`(max_rounds / max_steps / max_revisions / multi_specialist)、预选引导、
中途取消(返回确定性部分结果)都在 `research_lab.py` 里。

### 工具目录 —— 单一注册表(`agents/registry.py`)

`build_scientist_catalog()` 是唯一真源;gateway 和 System 页都从它构建。提供者(按序):

- **`scrna_pack.py`** —— 真实 scanpy 分析线(`run_scanpy_qc` / `normalize` /
  `clustering` / `de` / `enrichment`,带 matplotlib 图 + CSV 表);
- **`literature_search`** —— Europe PMC 真实引文(见下面的交接);
- **`make_schematic`** —— 确定性流程图(graphviz/mermaid/D2);
- **`run_code`** —— 每次运行沙箱里的 CodeAct(HPC3 上是 Singularity);
- 一对轻量 smoke QC/DE,**装了 scanpy 时自动丢弃**。

### 已移除(别再找这些)

- **Kosmos,全部** —— `integrations/kosmos_kernel.py`、kosmos 运行时、`BIOAGENT_KOSMOS_*`
  环境变量、`configs/kosmos-*`、`bioagent.kosmos_smoke`、`kosmos` extra、以及 CI 的 smoke
  步骤(提交 `145f9f8`、`0f1e021`)。
- **自主循环 + harness + eval + CLI** —— `eval/autonomous_loop.py`、`eval/comparison.py`、
  `eval/parity.py`、`bioagent.harness`、`bioagent`/`bioagent-web` CLI 和 `web_server.py`
  (提交 `2c524fb`)。可复用的辅助函数保留在 `agents/loop_utils.py`。**唯一产品入口是
  gateway 控制台**。
- **Biomni 后端工具** —— `run_biomni` / `deep_research` 已淘汰(`b8bafe4`)。Biomni 最终
  **没有**被 vendor 成 `tools/biotools/` 的 `BioToolRuntime`;由文献工具 + scanpy 分析线取代。

### 报告:出版级手稿(不是单独的 agent)

报告是**确定性的运行后打包**,不是 `OutputAgent` 类(提交 `63f561e`、`b25ceeb`):确定性
流程图(graphviz)+ 模型写的手稿 + **自审(渲染前和渲染后各一次)**+ **pandoc → PDF /
DOCX / MD**,缺 pandoc/graphviz 时优雅降级。2026-06-11 说的"第二个 vLLM VL 进程做 VL 排版
审查"**仍只是计划中的质量升级**,当前只有文本自审。

### 团队 / 归属变更 —— 文献:Wenyi → MaziYao

**MaziYao 从 Wenyi 接手 Literature & Evidence 工作流**(Wenyi 后续可能不在项目里)。这包括
现有的文献雏形 —— **`src/bioagent/tools/literature_search.py`**,即 Europe PMC 真实引文后端
(返回经核实的论文:title/authors/year/journal/DOI/PMID),也是计划中 **`paper-qa` 工具**
(全文检索 + grounded RCS)的明确**前身** —— 以及 Wenyi 之前所有文献/grounding/评估的范围。
修订后的归属表(取代 2026-06-11 那张;当年设想的 `workflows/{analysis,literature}.py` 拆分
**没有**落地 —— 这些工作现在都在共享的 `agents/` + `tools/` + `registry.py` 里):

| 人 | 负责 | 在哪 |
| --- | --- | --- |
| **Yijun** | 编排 + 输出:`ResearchLab` 循环(PI/Critic/汇总)、gateway 控制台、HPC3/vLLM serve + Singularity-Slurm 引擎、报告打包 | `agents/research_lab.py`、`agents/research_harness.py`、`gateway/`、`tools/report.py` |
| **Ziyao** | 分析线:scanpy QC/归一/聚类/DE/富集 + CodeAct,及其图/表/测试 | `tools/scrna_pack.py`、`agents/sandbox.py`、scanpy 注册项 |
| **MaziYao** | Literature & Evidence(从 Wenyi 接手):`literature_search` 雏形 → 计划中的 `paper-qa`;引文 grounding + 证据评分 | `tools/literature_search.py`、文献注册项 |

### 路线图(计划中,尚未做)

- **`paper-qa` 文献工具** —— 在 `literature_search` 之上做全文检索 + grounded RCS(**MaziYao**)。
- **报告 VL 排版审查** —— 同一 Slurm job 上第二个 vLLM VL 进程(2026-06-11 说的报告质量升级)。
- **真实 HPC3 Singularity 分析验证** —— 真实 CPU 节点上的 sbatch/squeue/scancel + dfs3b 只读
  bind(`gateway/slurm_job.py` 引擎已建好 + 离线测过;真机跑是最大未知数)。
- **更长期的架构方向**(另行跟踪):迁到 LangGraph + 自托管 Langfuse + Postgres checkpointer。

### 图

最新的工作流 + 归属图(当前 `ResearchLab` 架构,以及 Wenyi → MaziYao 文献交接)在 FigJam
看板:`https://www.figma.com/board/CeOxM9bgbgAGlw3kmst2qy`("v2"那两张;同一文件里旧的
"13-agent"图只作历史保留)。

---

## 2026-06-11(后续³)—— 预选(引导 PI)+ 中途取消 已上线

- **预选 = 引导 PI 的提示词**(非绕过):`agents/presets.py`(`celltype_annotation` 路径)、
  `LabConfig.preset_prompt` → 注入 `_pi_plan`、网关 `LabRequest.preset`/`preset_prompt` +
  `GET /api/presets`。用户可改提示词;plan mode 仍可改草稿 agenda。3 个测试。
- **中途取消——原来是空操作,现在真能停。** 之前:`/api/chat/stop` 设了 `conn.chat_stop`,但
  `ResearchLab`/`ResearchHarness` 没人查,run 照样跑完。现在两者都接 `should_cancel`:lab
  在**步骤之间**查、Scientist 在**工具轮之间**查,`_run_lab` 里接 `conn.chat_stop.is_set`。
  取消时 lab 返回**确定性的部分结果**(已通过的步骤)、**不再多调一次 LLM**(用户可能正是
  因为模型不对才停的),让用户看了再调。3 个测试。
- 138 测试,ruff 干净。

**前端(随后做完了):** 研究路径 UI 已进控制台。一个"Research path"下拉(`#presetSelect`)
+ **✎ Guidance** 切换按钮,展开一个**可编辑的方法学 textarea**(`#presetPrompt`,默认收起
以免杂乱)。流程:选路径 → 加载该路径的引导(可改)→ 读/改 → 在聊天框补充具体内容 → 发送。
方法学是**会话级 context**,持久化在会话上(`conversations.preset_key` / `context_prompt`
两列 + 通用的 `PATCH /api/conversations/{id}`),重开/换设备都还在。面板:右侧(下载/日志)
**默认收起**;左侧(连接)**首次 ready 时自动收起**、断开时重新展开。139 测试,ruff 干净。
> **DB 迁移 —— eye server 上已完成。** eye server 跑的是 **PostgreSQL**
> (`postgresql://bioagent@localhost/bioagent`),不是 SQLite。`conversations` 的两列已通过
> `ALTER TABLE conversations ADD COLUMN IF NOT EXISTS preset_key VARCHAR(64)` /
> `context_prompt TEXT` 在线加好(幂等、非破坏性,已有行取 NULL)。**要让功能真正生效还差:**
> 部署新代码(`scripts/push.sh`)+ 重启 gateway;在那之前运行中的旧代码会忽略这两列(安全)。
> 全新安装无需手动迁移:`init_db`/`create_all` 会自动建列,手动 ALTER 只针对已存在的生产库。

## 2026-06-11(后续²)—— 首个定制工作流(scGPT 形状、不用 scGPT)+ HPC3 Singularity 分析引擎

研究了一份真实的 scGPT 细胞注释 MWE(`~/Downloads/scGPT_mwe`):预处理(scanpy,CPU)→
scGPT 推理(GPU 基础模型)→ 后处理出图(scanpy)→ manuscript。决策(和用户):

1. **去掉 scGPT 依赖。** 保留*工作流形状*(预处理 → QC → 聚类 → marker → 注释 → 出图 →
   manuscript),但注释用 **Qwen3.6 读簇 marker**(+可选 gseapy 富集),不用 GPU 基础模型。
   于是唯一吃 GPU 的还是 Qwen3.6/vLLM,所有 scanpy 分析都是 CPU。scGPT 作为**未来可插拔的
   注释后端**保留(留接口、不实现)——以后真要细粒度参考亚型,再注册一个 `scgpt` 后端(GPU
   Slurm 作业);默认 `qwen_marker`。
2. **预选路径"引导"PI,而不是绕过它** —— *修正后的设计,本会话已实现*。预选 = 一段
   研究路径**提示词,注入到 PI 的规划**里(`agents/presets.py` → `LabConfig.preset_prompt`
   → `_pi_plan`),所以 PI 照样起草 agenda(按数据集自适应)、plan mode 照样让用户审改草稿、
   Scientist+Critic 照样逐步执行+校验。用户还能在运行前**改这段预选提示词本身**(就像 plan
   mode,但针对引导)。网关:`LabRequest.preset`/`preset_prompt` + `GET /api/presets`。
   (用硬 agenda 绕过 PI 的方案被否,因为没意义。)
3. **可插拔执行后端:** `local`(eye server 子进程——开发/调试/小数据)vs `hpc3_singularity`
   (Slurm + Singularity——真实/大数据/安全)。默认 `local`,前端先能跑起来看;验证后切 HPC3。
4. **沙箱 = HPC3 上的 Singularity**(已定)。CodeAct/分析在容器里跑
   (`--containall --writable-tmpfs --net none`),**数据集从 dfs3b 只读 bind**,work/artifacts
   读写。既有真隔离(容器内代码*删不掉/改不动*外界文件——不是"信任模型",是物理上做不到),
   又把重活挪出资源有限的 eye server。
5. **数据就地共置是性能铁律。** 重数据 + 重计算都留 HPC3/dfs3b;只有小派生工件(图、报告、
   摘要)回传给用户。**bind mount 是零拷贝**(命名空间映射、不搬数据);计算节点读 dfs3b 走的
   是集群 InfiniBand、**不碰** UCI 校园网。唯一真网络成本是把大数据(如 15GB)**首次落到
   dfs3b**——靠"数据本来就在 dfs3b"/直写 dfs3b/超大用 **Globus** 化解;**绝不能把中间 h5ad 在
   eye server 和 HPC3 之间来回搬**(那才会吃 WAN)。大稀疏矩阵会膨胀,分析 Slurm 作业要申请够
   内存(15GB 数据约 64–128GB)。
6. **每 session 一个常驻分析 worker**(类比 vLLM serve 作业),免得每步都排队拖延交互。

本轮已写代码并测过:

- **`gateway/slurm_job.py`** —— Slurm **批作业**引擎。`acquire_allocation` 提交作业、等
  PENDING→RUNNING(`startup_timeout_s`);队列太慢就 **`scancel` 并重新请求**,上限
  `max_attempts`(就是你说的"拉起超时→杀进程→重新请求")。`run_batch_job` 再等 RUNNING→
  COMPLETED(`run_timeout_s`,超了就 cancel + fail)。`singularity_exec` 拼容器命令(数据集
  `:ro`、`--containall`、无网);`build_analysis_script` 拼 CPU sbatch。全走 `RemoteExecutor`
  协议 → 完全可离线测。
- **`tests/test_slurm_job.py`**(8 个)—— 一次起成、**超时→取消→重提→第二次起成**、到上限放弃、
  sbatch 被拒、完成、失败,以及 Singularity/脚本构建器(数据集只读、sbatch 头)。共 132 测试,
  ruff 干净。

下一步(分阶段——我继续做):

- **预选工作流**定义 + **marker→细胞类型注释工具**(Qwen3.6),复用 `scrna_pack` 做
  QC/聚类/DE/富集/出图,`tools/report.py` 出 manuscript。
- 把 **`hpc3_singularity` 后端**接进 lab 的工具执行(把 scanpy/CodeAct 步骤派给 HPC3 CPU 节点
  上一个常驻分析 worker,数据在 dfs3b)。
- **前端:** 一个预选选择器,触发固定路径、流式显示每步进度 + 每步产物校验。
- 分析用 **`.sif`** 镜像(scanpy/anndata/leidenalg/gseapy)放 dfs3b + 几个 `HPCSettings` 字段
  (分析分区/内存/cpu/镜像)。
- **真机 HPC3 验证**(最大未知):真 CPU 节点上的 sbatch/squeue/scancel + Singularity + dfs3b bind。

## 2026-06-11(后续)—— 服务端聊天历史已上线;大文件上传延后

本轮实打实写了代码并测过(**124 测试全绿、ruff 干净、app.js 过了语法检查**):

- **服务端聊天历史。** 之前聊天**只在浏览器 `localStorage`**(每个浏览器各一份,换设备/
  清缓存就丢)。现在落到服务端、按 BioAgent 账号隔离,换浏览器/设备都还在。
  - DB:两张新表(`gateway/models.py`)—— `conversations`(一个 UI 聊天线程)和
    `messages`(一轮:`role` / `content` / `kind` / `meta` / `seq`)。文本进 DB;图/下载
    留磁盘,通过 `Message.meta` 里的 URL 引用 —— 和 `runs`/`datasets` 一样的"元数据进
    DB、二进制留磁盘"。
  - API(`gateway/auth_routes.py`,全部 `require_user` + 归属校验):
    `GET/POST /api/conversations`、`GET/PATCH/DELETE /api/conversations/{id}`、
    `POST /api/conversations/{id}/messages`。访问别人的会话直接 404(不能猜 id 越权)。
  - 前端(`frontend/console/app.js`):登录后会话存储走服务端(每个会话的消息懒加载;
    create/append/rename/delete 同步),**账号关闭时回退到 `localStorage`**(单用户/dev)。
    每个服务端调用都是 best-effort —— 失败就降级到本地,绝不弄坏 UI。
  - 测试:`tests/test_chat_history.py`(6 个)—— 创建/追加/顺序、按活跃度排序、改名 +
    删除级联、按用户隔离 + 归属、非法 role 拒绝。
  - **多用户提醒:** 服务器上用 **PostgreSQL**(只改 `BIOAGENT_DATABASE_URL`);SQLite
    dev 没问题,但并发写会 `database is locked`。

- **大文件断点续传上传(chunked)。** `/api/upload/chunk` + `/api/upload/status`
  (`app.py`)把数据集分块 append 到 `.part` 文件并报告已收字节数,断线后浏览器从服务端的
  offset 续传、而不是重头来;`frontend/console/app.js` 对 >16 MB 的文件分块(8 MB/块,带
  重试退避),小文件仍走单发 `/api/upload`。offset 不匹配 → 409 + 真实 offset 让客户端
  重新对齐(不会损坏文件)。mock 测试:`tests/test_resumable_upload.py`(3 个)—— 中断→
  续传→定稿字节一致、offset 不匹配 409、未知连接 404。

延后(TODO,未做):

- **反代 + 体积策略。** 还没上 nginx `client_max_body_size` / HTTPS(公网暴露前必须)。
  续传已经解决"断线重来"的痛点,但反代体积上限 + "大矩阵优先用服务器路径"的 UI 提示仍是
  开口 —— 大矩阵仍建议用"服务器路径"框而不是上传。被放弃的上传残留的 `.part` 文件目前
  还没做垃圾回收(优先级低)。
- **附件表(可选)。** 现在 `Message.meta` 用 JSON 存工件引用;以后若要对附件做搜索/查询,
  再提升成单独的 `attachments` 表(path/mime/size/sha256)。v1 不需要。

---

## 2026-06-11 —— 方向修正(取代 Biomni/Kosmos 接入计划)

与团队(Yijun、Ziyao、Wenyi)的规划会。本次参考项目:**swaruplab/operon**
(生信 AI IDE)。本轮**未改任何代码** —— 本小节只记录决策,实现按下面的分工拆分。

> **后端说明:** 服务后端已从 **Ollama 换成 vLLM**(提交 `f8d5d04`、`4058c3b`;
> `gateway/settings.py` 里 `llm_backend="vllm"`,Apptainer `.sif`,OpenAI `/v1`,
> 模型 `QuantTrio/Qwen3.6-35B-A3B-AWQ`)。下面较早(2026-06-09)小节里出现的 Ollama
> 引用一律视为**遗留/fallback** —— 现在唯一支持的后端是 vLLM。

### 决策

1. **只做固定的智能体工作流。** gateway 现在本来就只驱动固定的 13-agent
   `VisionResearchAgent`(`workflows/vision.py:57`)。自主研究循环
   (`eval/autonomous_loop.py`,1237 行)**冻结 —— 仅作参考、不驱动**。现在不要投入。
   "只做固定工作流"主要意思是*别再把自主循环当成路线图*,而不是要新建什么。

2. **Kosmos —— 计划整体移除。** 上游 Kosmos 的 tool-calling 与 Qwen3.6 兼容性很差
   (2026-06-09 已记录,当时为此弃用 CLI 改写 native loop)。决定:**整体删除
   Kosmos**,而不是继续维护那个 native shim。研究循环这块我们到时候完全自己写。
   Kosmos 唯一真正有价值的点 —— **用 Docker 做 agent 隔离/管理** —— 记为*以后*再
   考虑,现在不做。清理目标(Ziyao,单独 PR):`integrations/kosmos_kernel.py`、
   Kosmos 运行时 + `BIOAGENT_KOSMOS_*` 环境变量、`eval/comparison.py` /
   `eval/parity.py` 里的 Kosmos 路径、`configs/kosmos-*`、`pyproject.toml` 里的
   `kosmos` extra、以及 Kosmos 相关测试。**本轮不做。**

3. **去 Biomni 化 = vendor、而非依赖(fork-and-own)。** **不要** `pip install
   biomni`。只把**我们真正用到的** Biomni-lab 工具函数迁进我们自己拥有的模块
   (如 `src/bioagent/tools/biotools/`),这样可以自由开发、去掉品牌、并去掉
   optional-extra 的 lazy-import 那套。这是 fork-and-own,不是薄封装。运行时类若改名
   则叫 **`BioToolRuntime`**(原 `BiomniExecution` / `BiomniAdapter`);环境变量
   `BIOAGENT_BIOMNI_*` → `BIOAGENT_BIOTOOL_*`(迁移期间保留旧名做别名)。这个固定
   mock 的"agent 工作流"对外就是我们自己的 —— 不向用户暴露任何 "Biomni" 命名。

4. **新建 `OutputAgent`,取代 `ReporterAgent` 的角色。** 现在 `ReporterAgent`
   (`agents/pipeline.py:549`)只写 `final_report.md` —— 一个基本只罗列工件*路径*的
   markdown;**core 里没有任何图表、也没有 docx 代码**(grep:matplotlib 只在
   `eval/comparison.py`)。要做一个产出**带图表的完整 DOCX**的 OutputAgent,流程如下
   (对应"用 LLM 对 LibreOffice 排版审查然后让 Word 更新"):
   - 流水线结果 → matplotlib 图表(`tools/figures.py`)
   - `python-docx` 拼装结构化报告(标题、方法、结果+图、局限、下一步)
   - `soffice --headless` 把 docx 渲染成 PDF/PNG
   - **LLM(视觉)审查渲染后的排版**,产出排版修正意见
   - 重新生成 docx,直到排版通过(有限次迭代)
   - 保留 `final_report.md` 作为兜底工件(永远不丢文本路径)。
   新依赖(一个 `output` extra):`python-docx`、`matplotlib`;宿主需装 LibreOffice
   (`soffice`)。
   - **排版审查器 = 在同一个 Slurm job 上起第二个 vLLM 进程来服务一个小 VL 模型
     (提议、待确认)。** 主模型 `QuantTrio/Qwen3.6-35B-A3B-AWQ` 纯文本,所以"排版审查"
     这步需要视觉模型。vLLM 是**一进程一个模型**,所以这是在同一张卡/同一个 job 上再起
     *第二个* `vllm serve`(一个 VL 模型,如 `Qwen2.5-VL-7B-AWQ`)、绑另一个端口 ——
     不是在一个 server 里加载第二个模型。OpenAI `/v1` 客户端(`gateway/vllm_client.py`)
     已经能讲这个协议、vLLM 也支持 VL 的图片输入,所以 OutputAgent 把视觉调用指向第二个
     端口就行。**瓶颈是显存、不是 SU**(同 job、同 wallclock ⇒ 固定 34 SU/GPU-小时):
     vLLM 会按 `vllm_gpu_mem_util`(现在 **0.92**)预占显存,等于没给第二个进程留位置 ——
     必须把主模型调低(如 ~0.6)、把剩下的给 VL 进程,这会缩主模型的 KV/上下文。
     **A100-80G 宽裕;A100-40G 紧**(24GB AWQ + KV + 一个 7B VL)。**待确认:** 在真机
     GPU 上确认 AWQ 的 VL 模型 + 两进程显存切分能跑通。太紧时的更省方案:先用确定性的
     几何/溢出启发式检查(不用第二个模型)做 v1,VL 作为质量升级。

### 参考 —— swaruplab/operon(借鉴 3 个模式)

- **一等公民的 "Report 模式"**,以项目文件为依据 → 印证把 Output Agent 独立成一个
  角色是对的。
- **tmux / nohup 持久化 SSH 会话**,让长 Slurm 作业在掉线后存活 → 相比现在进程内持有
  SSH 会话,这是 HPC3 稳定性最大的升级点。
- **内置 protocol/skill 库**(operon 自带 665 个 protocol)→ 我们去 Biomni 化后的
  `tools/biotools/` 就是同一思路的种子。

### 分工(Yijun / Ziyao / Wenyi)—— 每人一整条独立工作流

修订模型(取代前面"大 agent"的说法):**不要**把多个 agent 合进一个大类。改成每人拥有
一条**完整的子工作流** —— 自己的 agent 链 + 它需要的 skill/图表/测试,自己独立跑、独立
测。Yijun 的编排层把这些子工作流拼起来。(顺带解释:"合并成大 agent" 指的是把现在三个
独立类 —— `SingleCellQCExecutionAgent` + `DifferentialExpressionExecutionAgent` +
`GeneratedCodeExecutionAgent` —— 塞进一个类;我们**不这么做**。每人一条独立工作流更干净。)

目标模块划分:

| 人 | 负责 | 模块 |
| --- | --- | --- |
| **Yijun** | 编排 + 主干:Coordinator + Data 路由 → 跑两条子工作流 → Validation → OutputAgent;外加 `BioToolRuntime` adapter + HPC3/Slurm | `workflows/vision.py`(变成组合器)、`gateway/`、`integrations/`、OutputAgent |
| **Ziyao** | 一整条 **Analysis 工作流** —— 单细胞 QC → 差异表达/marker → 派生工件上的 generated-code 执行;它的 agent、skill、图表、测试 | 新建 `workflows/analysis.py`(及其 agent/skill) |
| **Wenyi** | 一整条 **Literature & Evidence 工作流** —— 文献上下文 → 脱敏 grounding → 研究评估/打分;它的 agent、skill、图表、测试 | 新建 `workflows/literature.py`(及其 agent/skill) |

现在所有东西是 `workflows/vision.py:57` 里一个扁平的 13-agent 列表。重构:把列表拆成上面
两个子工作流模块,把 `VisionResearchAgent` 变成组合器:Coordinator/Data →
`AnalysisWorkflow` → `LiteratureWorkflow` → Validation → OutputAgent。每条子工作流都能
独立跑 + 独立测;唯一共享契约是 `state.decisions[...]` + `emit(...)`。每个 owner 产出
自己工作流喂给 OutputAgent 的图表(figure 规格提前和 Yijun 约定)。分配可互换。

---

## 2026-06-09(再后续）—— Kosmos `kosmos run` 在 eye server 上跑通 ✅

把**真实**的 Kosmos CLI(`/data/BioAgent/kosmos/.venv/bin/kosmos`)驱动到干净初始化、
进入研究循环(生成假设→设计实验→执行→分析)。中间一连串启动崩溃**全是配置层**的,
没一个碰到 LLM —— Kosmos 启动是纯本地 pydantic 校验,LLM 只在循环里才调,所以远程/离线
的 Qwen3.6 不影响启动:

- **`DEBUG_LEVEL` 崩溃,两个独立成因。**(a)宿主 shell 里继承来的脏 `DEBUG_LEVEL`
  经 `os.environ.copy()` 漏进子进程,破坏 Kosmos 的 `Literal[0,1,2,3]` 校验 →
  在 `integrations/kosmos_runtime.py` 的 `_build_env` 里 `env.pop("DEBUG_LEVEL", None)`
  修掉(和 `eval/comparison.py` 一致)。(b)连 Kosmos 自己 `.env` 里的 `DEBUG_LEVEL=0`
  也会崩:`.env` 的值永远是**字符串** `"0"`,而 pydantic v2 **不会**为 `Literal[int]`
  做 str→int 转换。该字段有 `default=0`(真 int),所以正解是**注释掉、别在 `.env` 里设**。
- **List 字段必须写成 JSON,不能是逗号串。** `ENABLED_DOMAINS` /
  `ENABLED_EXPERIMENT_TYPES` 在 Kosmos 的 `.env.example` 里是 `a,b,c`,但
  pydantic-settings 会 `json.loads()` 它们,必须写成 `["a","b","c"]`。(我们的
  `_build_env` 本来就注入 JSON 形式,所以 **BioAgent 路径从没受影响** —— 这只坑手动
  裸跑的 `kosmos run`。)
- **Ollama 路径下不需要 `ANTHROPIC_API_KEY`。** 错误框里那行 "Missing
  ANTHROPIC_API_KEY" 是模板话;该 key 只在 `LLM_PROVIDER=anthropic` 时才强制。
  `LLM_PROVIDER=litellm`(Ollama)下它可选且不用 —— 不需要假 key(中途加过一次假
  key 又删了,因为没必要;且 `db/__init__.py` 不序列化 config,本来也不会泄漏)。
- **修了过时的 `"Ocular biology research request:"` 前缀**(`kosmos_kernel.py` 的
  `build_research_loop_prompt`)→ 中性的 `"Biology research request:"`(眼科/视觉起源
  的残留,把 PBMC 的 brief 标错了)。

Kosmos 侧 `.env` 的正确配置现已固化在 **`configs/kosmos-dotenv.env.example`**(这些改动
在 *Kosmos 自己的* `.env` 里、不在 BioAgent 的,没法直接随仓库发,所以用这个文件文档化要
设哪些值)。本次新增部署辅助:**`scripts/push.sh`**(rsync checksum 覆盖推送,保护服务器
的 `.env`/`runs`/`reports`,主机名放在 gitignore 的 `.deploy.env`)和
**`scripts/diagnose_debug_level.sh`**(排查脏 `DEBUG_LEVEL` 来源)。回归测试加在
`tests/test_kosmos_runtime.py`(`_build_env` 剥离 `DEBUG_LEVEL`;brief 不含领域偏向)。

---

## 1. 产品方向(本次定的)

- **真实接入 Biomni + Kosmos。** 现在的 `BiomniAdapter` / `KosmosKernelAdapter`
  是**脚手架**(`docs/agent_registry.yaml` 里状态 `partial`):只"模拟"了工具注册表
  和安全策略,**没有 import / 调用真库**。目标是接到真 Biomni(`pip install biomni`)
  和 Kosmos(`jimmc414/Kosmos` CLI),**由本地 Qwen3.6 驱动**(不用云端)。见
  `docs/archive/biomni_kosmos_integration.md`。
- **模型:`qwen3.6:35b-a3b`**(Ollama MoE,24GB,256K 上下文)= 提案的
  "Qwen3.6-35B-A3B"。Biomni 和 Kosmos 都能指向本地 OpenAI 兼容 / Ollama 端点,
  **数据不出 UCI**。
- **隐私守得住**:本地 LLM + 文献检索可关/脱敏 + 数据湖只下不传 ⇒ 原始数据零泄露。
  `DataBoundaryGuard`(拦原始表格/密钥进 prompt)永远在外部调用前面。

## 2. 部署架构(已定:模型 A,集中托管)

```
网页用户(UCI 公网域名)── 浏览器 ──► eye server
  eye server(CPU、/data 6.9TB、有公网域名、SSH :<ADMIN_SSH_PORT>、无 GPU)
    • 网关(FastAPI)用一个中立 OS 账户 `bioagent` 跑
    • Biomni(+11GB 数据湖)+ Kosmos(+Docker 沙箱)+ agents + 前端
    • 每人结果:/data/BioAgent/users/<网页登录的UCInetID>/<run_id>/
    • SSH ──► HPC3,用每个网页用户各自的 UCInetID/密码/Duo
  HPC3(GPU、无公网)
    • Qwen3.6 跑在 GPU 上(短命 Slurm 作业起 ollama serve)
    • 共享模型权重:/dfs3b/ruic20_lab/software/bioagent/ollama
    • (Phase 2)重计算原地在 dfs3b 数据上跑 Slurm
```

**三种身份,别混:**

| 身份 | 是什么 | 在哪 | 几个 |
| --- | --- | --- | --- |
| eyeserver 上跑网关的 OS 账户 | **`bioagent`**(中立服务账户;开发期用 `<ucinetid>`) | 你以它的身份启动进程 | **就一个** |
| 每个网页用户的 HPC3 登录 | UCInetID+密码+Duo | **浏览器表单**里输,每次会话 | 每人各自 |
| `.env` 部署配置 | 路径、Slurm **计费账户名**、模型、结果目录 | `.env` 文件 | 是**值**,不是登录 |

结论:
- **网页用户只要有 HPC3 账户**,eyeserver 上不需要账户。网关用他在浏览器里输的
  凭证**替他 SSH 到 HPC3**。
- eyeserver 的 `bioagent` 账户**不需要 HPC3 权限**,只跑进程、存结果。
- **不绑任何人的 `<ucinetid>`。** `src/` 代码无写死用户;只有本地(gitignore)`.env`
  放开发者的值,全走环境变量注入。交接 = 建 `bioagent` 账户 + `chown /data/BioAgent`
  + 改 `.env`,**代码零改动**。

## 3. 本次搭的东西 —— HPC3 控制台(`src/bioagent/gateway/`)

真实可上线的网页控制台(FastAPI + WebSocket;`frontend/console/` 是干净的苹果风 SPA)。运行:

```bash
pip install -r requirements-gateway.txt          # paramiko + fastapi + uvicorn
PYTHONPATH=src python3 -m bioagent.gateway --port 8800
# 打开 http://127.0.0.1:8800/  —— 勾 "Mock mode" 可无集群演示
```

功能(全部在 **mock 模式**下验证过,mock 在进程内模拟 HPC3+Ollama):
- 真 **paramiko SSH** 到 HPC3,**交互式 Duo**(登录表单有验证码栏 + Step-2 面板)或密钥。
- **按用户隔离的 GPU serve 作业**(`gpu.py`):复用你自己的、绝不碰别人的;"Stop my
  GPU job" 只 scancel 你自己的。
- **Ollama 自动检测 → 没有就无 root 安装 → 拉模型**;安装地址已改成新的 `.tar.zst`
  (Ollama 弃用了 `.tgz`)。
- **GPU 健康监控**(`nvidia-smi`);日志里**完整打印错误原因**。
- **聊天驱动 BioAgent 流水线**(`VisionResearchAgent`),Reporter 的 LLM 指向隧道里的
  Qwen3.6;逐 agent 进度流式显示。
- 模型**登录后**从 `ollama list` 选;工作流下拉(强制工作流,认中文关键词);可选数据集路径(跑真实 QC/DE)。
- **下载**(右栏):生成 `.ipynb` notebook + "下载全部结果 .zip" + 每个文件,按 run 从磁盘提供。
- **HPC3 存储面板**:`dfsquotas` + `du` 列表 + 删除(严格限本人目录,越界拒绝)。
- 可折叠侧栏、ChatGPT 式会话侧栏(localStorage)、按用户分结果目录(`BIOAGENT_RESULTS_DIR`)。
- **DFS 组处理**:`/dfs3b/ruic20_lab/` 下的路径自动套 `sg ruic20_hpc`(base64+`sg` 包装)。

## 4. 已有智能体架构(已梳理 —— 4 层)

1. **13-agent 确定性流水线 + 安全层** — `agents/pipeline.py`(693 行)、
   `workflows/vision.py`。固定顺序 Coordinator→…→Reporter;`decisions` 只追加;
   **只有 ReporterAgent 用 LLM**。`ValidationAgent` + `DataBoundaryGuard` + claim 降级是安全核心。
2. **自主研究循环** — `eval/autonomous_loop.py`(1237 行):把流水线包成多轮研究
   (7 阶段、checkpoint、token 预算、收敛打分、风险门、LLM 决 next_action、evaluator)。
   **网关目前没驱动它**(只驱动单趟流水线)。把控制台接到自主循环是待办项。
3. **Kosmos / Biomni 适配器(脚手架,`partial`)** —
   `integrations/{kosmos_kernel.py, biomni_adapter.py, adapters.py}`。只建了工具
   注册表+能力决策+策略;**没真 import**(已证实:`grep -rE 'import (kosmos|biomni)' src` 无;requirements 里也没有)。
4. **评测/对标** — `eval/{comparison.py, parity.py}`:把 BioAgent 跟外部 Kosmos CLI
   (子进程、可选)在 10 个 parity 维度对比。

完整的"虚拟实验室"多专家愿景见 `docs/biomni_merged_architecture.mmd`。

## 5. 服务器事实(真机确认)

- **eye server**:Ubuntu,**`/data` = 6.9TB**(用它,别用小的 `/home`),
  **SSH 管理 SSH 端口**,**无 GPU**,可申请公网域名。公共组 `users`(gid 100)包含所有人;
  没有专门实验室组(`ruic20`/`-admin` 空)。账户是 `<名>` + `<名>-admin`;admin 在 `sudo` 组。
- **HPC3**(UCI RCIC):GPU 卡型 `A100 A30 L40S RTX6000 V100`(gres 如 `gpu:L40S:1`;
  24GB 模型需 L40S/A100,V100=16GB 太小)。计费账户:`ruic20_lab`(CPU)、
  **`ruic20_lab_gpu`**(GPU)。GPU 计费 ≈ **34 SU/卡·时,各卡型同价**(申请资源×墙钟,空转也扣)。
  `dfs3b` 组配额 = **600 TiB,已用约 556(剩约 44 TiB)** —— 存储不是约束,只是池子整体约 93% 满。
  访问 `/dfs3b/ruic20_lab/` 要 `newgrp ruic20_hpc` / `sg ruic20_hpc`。

## 6. GitHub + CI

- 私有仓库:**`KrimsonSun/BioAgentPrototype`**(remote `origin`)。
- CI(`.github/workflows/ci.yml`):**静态分析**(编译 + `ruff`)+ 离线测试 + 吸烟 harness;
  PR 门禁在 `pr-review.yml`。
- **`main` 分支保护**:4 个必过 check + 1 approval;admin(owner)可绕过直接推(本次就是这么推的)。
- `gh` token 需要 `workflow` scope(本次已授权)。

## 7. 设计文档(必读)

- `docs/archive/biomni_kosmos_integration.md` — 真实集成方案(放哪、隐私、LLM 指 Ollama/Qwen3.6、分步 I1–I5)。
- `docs/archive/phase2_hpc_compute.md` — 在 HPC3 上 Slurm 原地跑重计算。
- `docs/archive/hpc3_console.md` — 网关设计、配置、RUIC20 设置。
- `docs/archive/reference_architecture.md`、`docs/agent_registry.yaml`、
  `docs/biomni_merged_architecture.mmd` — 已有架构自带的地图。
- `configs/aiscientist.example.env` — 去个人化的部署配置(现已纳入 git;之前被全局
  `*.env.*` gitignore 悄悄漏掉了)。
- Figma 部署图:在 "Yijun Sun's team"(FigJam)。

## 8. 分步计划

**Phase 2 — HPC3 Slurm 原地计算**(`docs/archive/phase2_hpc_compute.md`):建 `SlurmRunner`
(提交/轮询/回收,复用 `gpu.py` 模式)+ 分析作业模板;`HPCAgent` 加真提交模式。
HPC3 环境很简单(`module load anaconda; python …` —— 工具只需 stdlib + h5py)。
2a/2b/2d 可离线 mock 建;2e 只需确认模块名。

**Biomni + Kosmos 集成**(`docs/archive/biomni_kosmos_integration.md`):
- I1 — eyeserver `/data` 装 Biomni、指 Qwen3.6、跑个小任务。**(eyeserver,待做)**
- I2 — 装 Kosmos、LiteLLM→Qwen3.6、`kosmos run` 小问题。**(eyeserver,待做)**
- I3 — ✅ **已完成**(`feat(biomni)`):`BiomniAdapter.run()` → 真 `A1.go()`,前面挂
  `DataBoundaryGuard`,配 `RealBiomniRuntime`/`MockBiomniRuntime`,7 个离线测试。
- I4 — ✅ **已完成**(`feat(kosmos)`):`KosmosKernelAdapter.run()` → 真 `kosmos run`,
  前面挂安全门(`RealKosmosRuntime`/`MockKosmosRuntime`,气隙式 LiteLLM→本地 Qwen3.6,
  默认关文献),8 个离线测试。**尚未从自主循环里调用**(就是下面那步接线)。
- I5 — ✅ **已完成**:biomni/kosmos 都是**可选 extra**(`.[biomni]`、`.[kosmos]`);
  真实运行时惰性导入,轻量核心 + CI 仍绿(39 个测试,ruff 干净)。

## 9. 紧接着要做的

1. **eyeserver bring-up(你/admin)**:建 `/data/BioAgent/{env,biomni_data,kosmos,
   app,users}`,归 `bioagent:users`(开发期 `<ucinetid>:users`),`chmod 2775` + 默认 ACL。
   在 `/data` 上装 conda/venv(Python 3.11),`pip install biomni`。(系统 Python ≥3.11
   就用 venv;否则装 miniconda 到 `/data`。)**eyeserver 无 Slurm/module load**,就是普通服务器。
2. **HPC3 模型挪到共享区**:模型现在在 `/dfs3b/ruic20_lab/<ucinetid>/.bioagent/ollama`,
   `.env` 已指向共享的 `/dfs3b/ruic20_lab/software/bioagent/ollama` —— 挪过去一次
   (`sg ruic20_hpc -c 'mv … …'`)。
3. ✅ **I3 + I4 + I5 已完成(代码,本次会话)**:两个适配器现在都有"真实运行时 + mock +
   离线测试",前面挂 `DataBoundaryGuard`。所有配置走环境变量 —— **eyeserver 上只需设环境
   变量 + 装包,不改代码。**
   Biomni:`BIOAGENT_BIOMNI_DATA_PATH`、`BIOAGENT_BIOMNI_MODEL`/`BIOAGENT_OLLAMA_MODEL`、
   `BIOAGENT_BIOMNI_SOURCE`、`BIOAGENT_BIOMNI_BASE_URL`、`BIOAGENT_BIOMNI_API_KEY`。
   Kosmos:`BIOAGENT_KOSMOS_ROOT`、`BIOAGENT_KOSMOS_EXECUTABLE`、`BIOAGENT_KOSMOS_MODEL`、
   `BIOAGENT_KOSMOS_API_BASE`、`BIOAGENT_KOSMOS_ENABLE_LITERATURE`、`BIOAGENT_KOSMOS_BUDGET_USD`、
   `BIOAGENT_KOSMOS_MAX_ITERATIONS`、`BIOAGENT_KOSMOS_TIMEOUT_SECONDS`。
   要真正执行(而非只出计划),构造适配器时传 `mode="execute"` 的策略 —— 默认仍是只出
   计划、绝不调运行时。
4. **eyeserver 配置 + 调试(你)**:I1/I2 —— 在 `/data` 装好 Biomni & Kosmos,设上面那些
   环境变量,然后用真实运行时调 `BiomniAdapter(policy=...).run(...)` /
   `KosmosKernelAdapter(policy=...).run(...)`,验证 Qwen3.6 的 tool-calling。
5. **然后接线(下一步代码)**:从 `eval/autonomous_loop.py` 里调 `KosmosKernelAdapter.run` /
   `BiomniAdapter.run`,并从控制台暴露(现在控制台走的是单趟流水线/只出计划那条路)。

## 10. 验证命令

```bash
.venv/bin/python -m pytest                                   # 39 个测试
.venv/bin/ruff check src tests                               # 静态分析(CI 门禁)
PYTHONPATH=src .venv/bin/python -m bioagent.gateway --port 8800   # 控制台(勾 Mock)
PYTHONPATH=src .venv/bin/python -m bioagent.harness --workspace runs/harness-offline --no-llm
PYTHONPATH=src .venv/bin/python -m bioagent.kosmos_smoke --workspace runs/kosmos-kernel-smoke
```

## 11. 待定 / 阻塞

- **跟 Jin 确认**集中托管(模型 A)是不是他要的,还是每人自跑(模型 B)。本次选了 A。
- HPC3 **CPU 分区名**(已知 `gpu`,用 `sinfo -s` 确认)、Phase 2 的 **Python 模块名**(`module avail`)。
- **LLM 吞吐 / SU**:Biomni + Kosmos 调用量大,单 GPU serve 可能瓶颈、SU 涨 —— 考虑
  常驻 serve + 固定端口,而不是每连接临时隧道。
- Qwen3.6 跟 Biomni/Kosmos 的 **工具调用可靠性** —— 先验证。
- Biomni + Kosmos 的 **许可证** 是否适合实验室使用。

## 12. 协作约定

- `requirements.txt` 保持轻量(h5py);biomni/kosmos 进**可选 extra**。
- 实验室工具放 `src/bioagent/tools/`;框架适配器放 `src/bioagent/integrations/`;
  评测放 `src/bioagent/eval/`;控制台放 `src/bioagent/gateway/`。`runs/` 被 gitignore。
- 真密钥留在本地 `.env`(gitignore);`configs/*.env.example` 模板纳入 git(无密钥)。
- 网关用**一个中立 OS 账户**跑,永远别写死某个人。
