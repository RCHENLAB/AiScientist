# 交接文档 — `deep_literature`(PaperQA2 接本地 Qwen)

> ⚠️ **先读最上面的 `## 2026-06-28` 段** —— 它交接**新的 references 模块** + 定稿的远端 provider 决定
> (Edison/FutureHouse Crow),并列出文献线接下来要做的事。下面的 2026-06-19 段是原始 `deep_literature`
> 交接(仍然准确,且那个工具**保持不动**)。

---

## 2026-06-30 —— References 重写:不再做隐藏的最终 fallback 检索

作者:Codex(Ziyao 文献线)
状态:**已在本地实现;定向测试通过。** 这一节覆盖下面同一天较早的 `gather_references()` fallback 设计。

### 决定

最终报告阶段不再“偷偷补救”文献缺失:如果 agent 没有真的跑并接受 `literature_search`,最后不会再自己查
Europe PMC 来让 report 看起来有 References。新契约是:

- `literature_search.py` 负责所有 Europe PMC 检索、query 聚焦、弱结果过滤。
- `literature_references.py` 只负责把本轮已 accepted 的 `literature_search` citations 格式化并插进
  `## References`。
- `gateway/app.py` 不再 import 或调用 `gather_references()`。如果没有 accepted DOI/PMID-backed
  `literature_search` 结果,就插入诚实的空 References note,并在 technical report 里说明没有 hidden fallback。
- `ResearchLab` 增加确定性的 plan guard:如果用户要求 literature / references / citations /
  background / biological interpretation,但 PI 漏了文献步骤,代码会自动把低优先级步骤替换成
  `Literature search ...`。
- 文献类 agenda step 仍然会确定性硬路由到 `literature_search`,不再让 LLM Scientist 自己决定是否调用。

### 改动文件

- `src/bioagent/tools/literature_search.py`
  - 新增 `focus_literature_query()`。
  - 把 dataset/file/metadata stopwords 清理、generic/off-topic hit 过滤移到这里。
  - query 聚焦现在会删除 `Search`、`Return`、`citations`、`evidence` 这类工具指令词;
    例如 `Search DDX41 retina Return citations evidence` 会聚焦成 `DDX41 retina`。
- `src/bioagent/tools/literature_references.py`
  - 重写为纯格式层:`references_from_citations()`、`empty_references()`、
    `format_references_section()`、`insert_references()`、`degradation_note()`。
  - 不再负责 report-time 检索;不再调用 Europe PMC 或 remote provider。
- `src/bioagent/gateway/app.py`
  - 最终 References 只来自 `_references_from_accepted_literature_search(result)`。
  - 如果返回 `None`,最终报告记录本轮没有 accepted literature-search citations。
  - report writer/reviewer prompt 现在禁止在正文里写 `Title:`、`Authors:`、`DOI/PMID:`
    这种 bibliography-style 文献块;最终 finalize 时也会在重新插入权威 `## References` 前,
    把正文里的这些 metadata 行删掉。
  - Literature Review 正文现在会清掉 hallucinated figure callout,例如
    `(see Figures 1-2 for cited figure references)`,同时保留 Results 里正常的数据图引用。
- `src/bioagent/agents/research_lab.py`
  - 新增 requested-literature 的确定性 agenda 修复。
  - 文献 step 仍然硬路由到 `literature_search`。
  - 重复文献 agenda item 现在会压成一个标准 `Literature search for ...` step,包括模型吐出
    `literature_search` + 一句自然语言文献步骤的情况。
- Tests
  - `tests/test_literature_search.py` 覆盖 query 聚焦和 Europe PMC hit 过滤。
  - `tests/test_literature_references.py` 只覆盖格式化/插入。
  - `tests/test_research_lab.py` 覆盖 PI 漏文献步骤时自动补文献 step。

### 验证

- `PYTHONPATH=src python3 -m pytest tests/test_literature_search.py tests/test_literature_references.py tests/test_research_lab.py::test_pi_plan_guard_adds_literature_step_when_model_omits_it tests/test_research_lab.py::test_literature_context_step_is_deterministically_routed_to_literature_search tests/test_research_lab.py::test_literature_step_detection_does_not_match_background_rna_cleanup -q` → **19 passed**。
- `PYTHONPATH=src python3 -m pytest tests/test_literature_search.py tests/test_literature_references.py tests/test_research_lab.py::test_pi_plan_guard_adds_literature_step_when_model_omits_it tests/test_research_lab.py::test_pi_plan_guard_collapses_duplicate_literature_steps tests/test_research_lab.py::test_literature_context_step_is_deterministically_routed_to_literature_search tests/test_research_lab.py::test_literature_step_detection_does_not_match_background_rna_cleanup -q` → duplicate-plan 修复后 **21 passed**。
- `PYTHONPYCACHEPREFIX=/tmp/bioagent_pycache PYTHONPATH=src python3 -m py_compile ...` 对改动的
  Python 文件(包括正文文献 metadata 和 literature-figure 清理路径) → **通过**。
- gateway-specific tests 在当前本地解释器里仍因缺 FastAPI 无法跑;合并前应在项目/test 环境再跑 gateway suite。

### 新验收标准

文献功能是否成功,只看本轮是否真的调用并 accepted `literature_search`,以及最终 `## References` 是否复用这一步的
DOI/PMID-backed citations。最终 report 不再通过 hidden fallback search 来证明文献功能成功。

---

## 2026-06-30 —— 最终 References 复用已接受引用 + planner 现在会保留文献步骤

作者:Codex(Ziyao 文献线)
状态:**已在本地实现;文献引用相关定向测试通过。** 新增了 gateway bridge 的测试,但当前本地 Python
环境缺 FastAPI,所以 gateway 测试会被跳过;语法检查已通过。

### HPC 上暴露的问题

HPC run `16ff701c516c` 暴露了一个新的接线 bug:

- 计划里的文献步骤确实跑了,而且被 critic 接受。log 里能看到三次成功的 Europe PMC
  `literature_search`,query 包括 `DDX41 retina innate immunity`、
  `DDX41 RIG-I MDA5 antiviral signaling`、`DDX41 microglia retinal inflammation`。
- 这些 accepted step 结果里已经有真实 DOI/PMID 引用,例如 Mars et al. 的 DDX41 retinal dystrophy,
  以及 Sauter et al. 的 retinal RLR antiviral system。
- 但最终 manuscript 还是以这句结尾:
  `No external citations were retrieved for this run (literature retrieval unavailable).`

根因:最终写 `## References` 的逻辑**没有复用 lab run 里已接受的 `literature_search` citations**。
它总是重新调用 `gather_references(req.question)`。这次数据集 prompt 被最终 query 聚焦成了
`uploaded DDX41 DEG h5ad single-cell RNA-seq conditional knockout wild-type mouse`,明显比 accepted
literature step 的 query 差,所以最后可能搜空/过滤空。

### 改了什么

- `src/bioagent/gateway/app.py`
  - 新增 `_references_from_accepted_literature_search()`。
  - final report References 现在优先使用 accepted in-loop `literature_search` tool results。
  - 如果 scientist 的 accepted 文献总结里明确提到了 DOI/PMID,最终 References 只保留这些被总结过的论文,
    避免把 raw Europe PMC 里比较宽泛的结果一股脑塞进 manuscript。
  - 如果没有 accepted DOI/PMID-backed literature-search citation,才走原来的
    `gather_references(req.question)` fallback。
  - 新增写出 `process/literature_references.json`,以后结果包里能直接看到最终 `## References` 是哪条路径填的。
  - 当复用 accepted literature step 引用时,log 会发更明确的 success message。
- `src/bioagent/agents/research_lab.py`
  - 把 PI planning 规则从 optional 改成 conditional-required:如果用户要求 literature context、biological
    interpretation、background、references/citations,或者要求用已发表生物学背景支撑 report,PI 必须留一个
    `literature_search` agenda step。
  - prompt 也明确说:如果 5-step 上限会挤掉文献步骤,就合并/删掉优先级更低的描述性/annotation 步骤,不要删文献。
  - 新增文献类 agenda step 的确定性执行路由:如果 step 文本包含 literature/reference/citation/background
    这类词,ResearchLab 会直接执行 `literature_search`,不再让 LLM Scientist 自己选工具。这避免模型在失败的
    QC retry 里乱调文献,或者在真正的文献 step 里跳过文献工具。
- `src/bioagent/tools/literature_references.py`
  - `degradation_note()` 现在把任何 `status="ok"` 且有 citations 的结果都视为成功,包括新的
    `tier="lab_literature_search"` 路径。
  - 增加更多 dataset/file/metadata stopwords(`uploaded`、`h5ad`、`DEG`、`sampleid`、`majorclass`、
    `celltype` 等),避免最终 fallback query 把 UI/数据文件词送去 Europe PMC。DDX41 数据集 prompt
    现在会聚焦成 `DDX41 conditional knockout mouse retina`。
- `tests/test_gateway_lab.py`
  - 新增回归测试覆盖这次 HPC 失败模式:accepted `literature_search` citations 会被复用,rejected round
    的 citations 会被忽略,没有被 accepted summary 提到的 broad hits 不会进入最终 References。
- `tests/test_literature_references.py`
  - 新增测试:复用 accepted lab citations 时,技术报告不会误报成 "NO citations"。
  - 新增 dataset prompt cleanup 覆盖,确保 `DDX41_DEG.h5ad`、`sampleid`、`majorclass`、`celltype`
    不会进入最终 fallback 文献 query。
- `tests/test_research_lab.py`
  - 新增测试:PI system prompt 带有 conditional-required `literature_search` planning 规则。
  - 新增测试:`Summarize findings with literature context` step 会绕过模型工具选择,确定性调用
    `literature_search`,并在 step final_answer 里返回 final References 复用所需的 DOI。
  - 新增 guard 测试:普通分析措辞如 `background RNA correction` 不会误触发文献 route。

### 验证

- `PYTHONPATH=src python3 -m pytest tests/test_research_lab.py::test_literature_context_step_is_deterministically_routed_to_literature_search tests/test_research_lab.py::test_literature_step_detection_does_not_match_background_rna_cleanup tests/test_research_lab.py::test_pi_system_prompt_carries_design_aware_rules tests/test_literature_references.py -q` → **21 passed**。
- 对改动的 Python 文件跑 `PYTHONPYCACHEPREFIX=/tmp/bioagent_pycache PYTHONPATH=src python3 -m py_compile ...`
  → **通过**。
- gateway-specific test 在当前本地解释器里因为缺 `fastapi` 没法实际跑;合并前应在项目/test 环境再跑一次。

### 剩余边界

这次只修最终 manuscript References 的接线。single-cell custom analysis step 里反复出现的 `run_code`
错误是另一条 core/orchestrator 分析流程问题,不属于这次文献线修复。

---

## 2026-06-30 —— Europe PMC 兜底质量修复(query 聚焦 + abstract book 过滤)

作者:Codex(Ziyao 文献线)
状态:**已在本地实现,并已通过 full UI pipeline 验证。** 定向测试已通过
(`tests/test_literature_search.py`、`tests/test_literature_references.py`,以及后续 gateway 接线
`tests/test_gateway_lab.py`)。这次不处理 Crow/Edison。

### 今天确认了什么

Europe PMC 本身已经实现,也已经接进报告流程。直接本地调用
`gather_references("What diseases are caused by germline DDX41 mutations?")` 可以返回带 DOI 的真实引用。
重新启动本地 gateway 后,新的本地 run `b8271a7f6a7a` 也证明了自动报告路径是通的:它生成了
`report/technical_report.md`,技术报告里记录了 Europe PMC fallback,最终 `report.md` 也插入了编号格式的
`## References`。

剩下的问题不是"没联网/没接上",而是**引用质量**。自动 References 模块以前把整段用户指令直接当 Europe PMC
关键词搜,例如 "Write a short literature-only report ... Do not run QC ... Include real references"。Europe PMC
只是关键词 API,不是 LLM,这种 UI/写作指令会把搜索带宽,导致排在前面的可能是 annual meeting abstracts /
conference abstract book,而不是具体的 DDX41 疾病综述论文。

后续 full dataset run 又暴露第二个 report-pipeline 问题:草稿先拿到了好的自动引用,但 LLM self-review/report
rewrite 可能把它改成弱引用,例如 "Publication Only",或者改成虽然有 DOI 但不相关的 CAR T AML 引用。下面的修复让
automatic references module 成为渲染前最后一个写 `## References` 的模块。

### 改了什么

- `src/bioagent/tools/literature_references.py`
  - 新增确定性的 `_focus_reference_query()`:短的科学问题保持不动;长的任务/报告指令会先压成短的生物医学 query,
    例如 `diseases caused germline DDX41 mutations`。
  - 只在自动 References 路径里清洗 Europe PMC 结果:过滤掉 generic meeting/conference abstract books,并优先保留
    有 DOI/PMID、标题/metadata 命中 `DDX41`、`germline`、`mutations` 等 query 词的结果。
  - 收紧 full-pipeline query 清洗:不会把 `Important`、`must`、`populated`、`weak placeholders`、
    `relevant biology`、`invent` 这类最终报告指令词发给 Europe PMC。现在 full pipeline prompt 会聚焦成
    `DDX41 retina innate immunity hematopoiesis`。
  - 新增 off-topic 过滤:即使有 DOI,如果 title/journal/authors 完全不命中 focused query term,也会丢掉。
    这解决了 DDX41 retina 报告里反复混入 CAR T AML 引用的问题。
  - 把 `Publication Only` 当成 generic/non-useful citation title 过滤。
  - `insert_references()` 现在会在插入权威 `## References` 前清理模型自己写的非 H2 References 段
    (例如 `### References`),避免最终报告出现重复 References。
  - `gather_references()` 返回值新增 `query`、`unfiltered_count`、`filtered_count`,以后能直接看到实际发给
    Europe PMC 的 query 和过滤掉多少条。
  - `degradation_note()` 会把 focused query 和过滤计数写进技术报告,以后不用靠猜。
- `tests/test_literature_references.py`
  - 新增测试:长报告指令会先聚焦,不会原样发给 Europe PMC。
  - 新增测试:generic abstract book 会被过滤,具体 DDX41 论文会留下。
  - 新增测试覆盖 full-pipeline citation 指令词清洗、off-topic DOI 过滤、`Publication Only` 过滤、
    重复/残留 References 段清理。
- `src/bioagent/gateway/app.py`
  - full pipeline 后续修复:report self-review 仍可能改坏或替换已经填好的 `## References`。
    现在 gateway 会在 self-review **之后**再跑一次 `insert_references()`,确保最终 manuscript citations
    的最后写入者是 references 模块。
- `tests/test_gateway_lab.py`
  - 新增回归测试:即使 reviewer 把 References 改成弱/疑似编造引用,最终渲染前也会恢复为 literature
    模块给出的 DOI-backed citations。

### UI 验证

- Full dataset run `fd2f94412a28` 证明 self-review 后二次插引用已经能把弱引用替换成 DDX41/retina/immunity
  相关引用,但仍混入了一条 off-topic CAR T AML。
- 加上 off-topic filter 和 stray References cleanup 后,full dataset run `498deb43051c` 的最终
  `report.md` 只有一个 `## References`,且是两条相关 DOI-backed 引用:
  - Mars Z et al. (2026), biallelic germline `DDX41` variants causing retinal dystrophy /
    retinal homeostasis, DOI `10.64898/2026.01.28.26344834`。
  - Devasahayam Arokia Balaya R et al. (2025), DEAD/DEAH-box helicases in immunity, infection and
    cancers, DOI `10.1186/s12964-025-02225-9`。
  - 没有 `Publication Only`,没有 CAR T AML,没有重复的 `### References` 段。

### 边界

- 这次只改**自动填 manuscript References 的模块**(`literature_references`)。
- 不改底层 `literature_search` Scientist 工具,也不碰 `deep_literature`/PaperQA2。
- Crow/Edison 仍然先不管;只有显式设置 `BIOAGENT_LITERATURE_REMOTE_URL` 时才会走远端 Tier 1。
- Europe PMC 仍然只是 keyword metadata search,不是全文 RAG;这次是把兜底引用变准,不是实现 Mode B 前置检索。

### 剩余边界

- 这次修的是最终 manuscript References,不保证 single-cell DE/statistical analysis 本身正确。数据分析行为仍归
  core/orchestrator 线负责。
- Europe PMC fallback 仍只是 keyword metadata search,不是全文 RAG;如果要更强的证据综合,仍需要后续接
  Tier 1 Crow/Edison 或 Mode B/front-loaded literature。
- 真正的 literature-only preset 仍会改善 planning UX,但最终报告的 References 现在不再依赖 planner 是否记得
  写 literature step。

---

## 2026-06-28 —— references 模块 + 远端 provider 决定(交接给 Ziyao Ma)

作者:claude(在 Yijun 核心线上)—— 应 Yijun 要求,**为文献线**写的交接。
状态:**references 模块已落地 `main`**(commit `1de5ec6`、`ada3e8d`);远端 Tier-1 接入 + Mode-B 是留给你的活。

### 为什么交给你

PI 手稿写作器一直预留 `## References` 段,占位符写着 *"Citations to be inserted by the literature
module (PaperQA)."* —— **但从来没人填它。** 我把这个缺失的模块补上并接进报告流程了。但检索**后端**的选择
和下一个功能(把文献前置到写作里)是文献线的活,所以交接给你。**你原有的 `deep_literature` 工具按要求一行没动。**

### 已落地(已在 `main`)

- **`src/bioagent/tools/literature_references.py`**(新)—— 用**真引用**填手稿的 `## References` 槽。分级、
  绝不编造、绝不抛异常(和 `literature_search` 同样的优雅降级契约)。导出:`gather_references()`、
  `format_references_section()`、`insert_references()`、`degradation_note()`。
- **`src/bioagent/gateway/app.py`**(接线)—— 草稿写完后,在自审**之前**填引用;把 `degradation_note` 接进
  `_build_technical_report`,任何 fallback 记进**技术报告的「Diagnostics & failures」**段。**学术手稿照常渲染。**
  写作/自审 prompt 已更新为逐字保留填好的 References。
- **`tests/test_literature_references.py`** —— 11 个测试。全量 193 通过。
- **`docs/archive/literature_embedding_plan.md`**(新)—— 定稿方案,以它为准(取代旧的微信"选型清单")。

### 两个 Tier —— 以及对"这俩都是外部服务?我以为会用 grep"的诚实回答

**是的 —— 现在落地的两级都是外部网络服务,都不是本地 grep。**

| Tier | 是什么 | 外部? | embedding |
|---|---|---|---|
| **1(主)** | **FutureHouse / Edison Scientific 平台,agent Crow**(PaperQA2 内核) | ☁️ 外部云 | provider 侧 |
| **2(兜底)** | **Europe PMC REST API**(`literature_search.search_europepmc`) | ☁️ 外部(ebi.ac.uk)—— 关键词 API,**无 embedding** | 无 |

**为什么 Tier 2 是 Europe PMC 而不是本地 grep:** grep 需要一个*本地语料*去 grep,而我们没维护这种语料。
Europe PMC 是轻量、无 embedding、给真 DOI/PMID、生物医学的关键词兜底,不需要任何索引。它只在 Tier 1 挂了
**但主机仍有外网**时触发 —— 这是当初商定的范围(完全断网的 UCI 机器*两个*服务都连不上,而我们商定不处理完全离线)。

**⟶ 待决定(Yijun 正在重新考虑):要不要加一个真·本地的 Tier 3 = grep 一个缓存论文语料?** 它只在*两个*外部
服务都连不上(即真离线)时触发,需要在 HPC3 维护一份本地论文/摘要库供 grep。Yijun 之前的决定是"不处理离线"→
不要 grep。若改主意,要做的是:(a) 定本地语料是什么、放哪;(b) 在 Europe PMC 之下加一个 `_gather_grep()` 级,
返回相同的 citation 结构。**标给文献线拍板。**

### 文献线的待办(Tier 1 接入)

远端 Tier **env 门控、目前休眠中** → 实际现在跑的是 Europe PMC 兜底,直到你接上 Tier 1。开启 Crow:

1. **申请 Edison/FutureHouse API key**,并**确认其数据留存 / "query 是否用于训练"的政策** —— 合规前提。
   (我们只发**公开研究问题**,绝不发 synthesis;已与 JinLi 确认。但 provider 侧仍要核实。)
2. 选接入方式:
   - **快路(现在就能用):** 在 `edison-client`(前身 `futurehouse-client`)前面套个薄 REST wrapper,收
     `{query, top_k}` 返 `{answer, references:[...]}`,然后设 `BIOAGENT_LITERATURE_REMOTE_URL`
     (+ `BIOAGENT_LITERATURE_REMOTE_KEY`)。模块里的通用远端路已经认这个结构。
   - **稳路:** 拿到 key、看到真实返回对象后,写原生 `edison-client` adapter(lazy import + 优雅降级,仿
     `paperqa_search.py`)。我**没**硬绑客户端 —— 没 key 没法验证它的返回 schema。
3. 任一配好后,主路自动从 Europe PMC 切到 Crow;`degradation_note` 之后只在真出故障时才触发。

### 成本与限流(成本现在是明确优先项 —— 详见 `COST_AND_CACHING.md`)

- **Europe PMC(Tier 2)= 实质免费。** 无 key、不碰 paywall(只取 metadata/DOI)、不收费。限流是 EBI
  合理使用(高并发偶发 429);我们每份报告几次的量级根本不接近。兜底不会冒出账单。
- **Crow(Tier 1)= credit 计费。** 买 credits → API key → 每次调用扣 credits;有"generous free tier",
  Crow 比 Falcon 便宜。⚠️ **确切 $/credit + 免费额度未确认**(pricing/FAQ 页 404)—— 预算前先登录
  `platform.edisonscientific.com` 拿 live 数字。
- **降本杠杆:** 用 Crow 不用 Falcon;Europe PMC 先行 / Crow 按需;**本地"问题→答案"缓存避免重复查重复扣费**
  (这才是真正有用的"本地"组件 —— 省钱,不是 offline grep);每份报告封顶 Crow 调用数。
- 完整拆解 + 可转发的汇报:**`handoff/ziyao/COST_AND_CACHING.md`**。

### Yijun 想要的下一个功能(**未实现**,要他点头)

**Mode B —— 把检索前置到写作:** 让模型在写 **Introduction + Discussion** 时自己查文献并据此引用,让
References 成为"实际用过什么"的副产品(现状是"末端引用器" —— 先写后引)。方案见
`docs/archive/literature_embedding_plan.md` §4。属 `app.py` 里 `_REPORT_WRITER_SYSTEM` 的编排改造;
**Results/Methods 保持无文献。** 尚未开始。

### 指针
- 方案 + 决定:`docs/archive/literature_embedding_plan.md`
- 模块:`src/bioagent/tools/literature_references.py`
- 接线缝:`app.py` 的 `_run_lab`(填引用)+ `_build_technical_report`(降级说明)
- 你没动的工具:`src/bioagent/tools/paperqa_search.py`(见下面的 2026-06-19 段)

---

# (原始)交接文档 — `deep_literature`(PaperQA2 接本地 Qwen)

日期:2026-06-19
作者:ziyao(文献线)
状态:**代码完成,本地 mock 测试通过;真 Qwen 端到端尚未验证**(需要 HPC3 —— 见"待办")。

## 一句话总结

新增了一个 Scientist 工具 `deep_literature`,封装了 **PaperQA2**(Edison Literature 背后的开源引擎,
github.com/Future-House/paper-qa)。现有的 `literature_search`(Europe PMC)只返回*一串论文列表*,
而这个返回的是*带引用的答案*(PaperQA 的 RAG 流程:检索 → 收集证据 → 合成带 in-text 引用的回答)。

PaperQA 默认 LLM 和 embedding 都用 OpenAI。**这次工作的核心就是把这些全部指向我们的本地栈**,
保证数据不出 UCI:LLM → 本次运行的本地 Qwen vLLM 端点,embedding → 本地 sentence-transformers 模型。
只有 PaperQA 的公开文献检索会联网。

## 做了什么

四个文件(可直接发 PR):

- `src/bioagent/tools/paperqa_search.py` —— **新增**。工具本体 + 本地模型接线。
- `src/bioagent/agents/registry.py` —— 在 catalog 里注册 `make_paperqa_tool()`。
- `pyproject.toml` —— 新增可选依赖 `literature = ["paper-qa[local]"]`。
- `tests/test_paperqa_search.py` —— **新增**。7 个 mock 测试(无需安装 paper-qa)。

## 设计(沿用现有工具的约定)

- **工具形态。** `make_paperqa_tool()` 返回一个 `HarnessTool`,名字 `deep_literature`,
  `category="literature"`,`reads_private_data=False`。和其它工具一样自描述,registry / System 页
  自动识别。
- **本地模型接线(核心)。** `_local_endpoint(ctx)` 读取 `ctx.tunnel_port` 和 `ctx.model`
  ——和现有 `chat_tools` 路径用的是同一套每次运行的上下文字段——拼出
  `http://127.0.0.1:{tunnel_port}/v1`。`_build_settings(ctx)` 把它通过 LiteLLM `model_list`
  喂给 PaperQA 的 `Settings`,把 `llm` / `summary_llm` / `agent_llm` 全部钉到本地 Qwen,
  `embedding` 钉到本地 sentence-transformers 模型(`st-` 前缀,默认 `st-multi-qa-MiniLM-L6-cos-v1`,
  可用环境变量覆盖)。
  > 注意:早期草稿里读的是 `ctx.ollama_port`,但 `HarnessContext` 没有这个字段
  > ——所以它永远返回 "unavailable"、根本连不上模型。已修正为 `ctx.tunnel_port`。
  > 如果对比旧草稿,这是唯一要注意的 bug。
- **优雅降级。** paper-qa 很重,所以是懒加载。没装 → 返回 `status="dependency_missing"`
  (和 `scrna_pack` 对 scanpy 的处理一致);没有本地端点 → 返回 `status="unavailable"`;
  PaperQA 内部任何异常都被捕获,返回 `status="error"`。工具从不抛异常,不会搞崩整个运行。
- **环境变量开关(不硬编码服务器路径):**
  - `BIOAGENT_PAPERQA_PAPERS` —— PaperQA 读取的 PDF 目录(默认 `<workspace>/papers`)。
  - `BIOAGENT_PAPERQA_EMBEDDING` —— 本地 embedding 模型名。
  - `BIOAGENT_LLM_API_KEY` —— 给本地 vLLM 的占位 bearer(`sk-no-key-required`)。

## 隐私(硬要求)

- LLM(general / summary / agent)→ 走隧道连本地 Qwen。无任何 prompt 文本发往云端模型。
- embedding → 本地 sentence-transformers,进程内运行。无任何切块文本发往云端 embedding API。
- 联网**仅**发生在 PaperQA 的元数据/检索客户端(Crossref / Semantic Scholar / Unpaywall),
  发出的是公开书目查询 —— 绝不发数据集。
- `tests/test_paperqa_search.py::test_success_pins_models_to_local_endpoint` 是一道回归保险:
  它断言 LLM 的 `api_base` 是 `127.0.0.1`、embedding 是本地 `st-` 模型。
  谁要是把这些改回云端模型,这个测试就会变红。

## 测试 —— 覆盖了什么、没覆盖什么

mock 测试通过 `sys.modules` 伪造 `paperqa`(和 `test_literature_search` 伪造 `httpx.get` 一个思路),
所以整套测试本地就能跑:不装 paper-qa、不联网、不用 GPU —— 一个"假 Qwen"。
`python3 -m pytest tests/test_paperqa_search.py -v` → 7 passed。

它们覆盖的是**管道逻辑**:空问题报错、`dependency_missing` 路径、没隧道时 `unavailable`、
带引用答案的解析、本地端点/embedding 的钉定、异常不致命、工具自描述。

它们**不覆盖**:答案质量;也不保证解析函数匹配*真实*的 PaperQA 返回对象(测试里那个假返回结构是假设的);
也不验证 LiteLLM 模型名串和实际 vLLM 对不对得上。**这些只能用真 Qwen 跑一遍才能确认。**

## 待办(真 Qwen 集成 —— 需要 HPC3)

1. **在服务器上安装。** `paper-qa[local]` 已在 `pyproject.toml` 声明,但尚未装进
   `/data/BioAgent/env`。确认 `pip install paper-qa[local]` 在那边能干净装上(会拉 torch +
   sentence-transformers)。
2. **PDF 语料。** 决定实验室 PDF 放哪,设好 `BIOAGENT_PAPERQA_PAPERS`。没有论文,PaperQA 检索能力有限。
3. **模型名对齐。** 部署的模型是 `QuantTrio/Qwen3.6-35B-A3B-AWQ`(`BIOAGENT_VLLM_MODEL`)。
   确认 LiteLLM 模型名串(`openai/<ctx.model>`)和 vLLM 实际服务名(`--served-model-name`)一致,
   不一致就调一下。
4. **端到端跑一遍 + 审计。** 发起一个真实研究任务,让网关在 HPC3 起 Qwen + 开隧道,确认
   `deep_literature` 能返回带引用的答案。用 PaperQA `verbosity=3` 打印每一次 LLM/embedding 调用,
   **审计确认没有任何请求打到云端 API**。
5. **核对解析函数。** 拿真实 PaperQA 返回对象校验 `_extract_answer`(不同 paper-qa 版本字段名可能不同),
   有出入就调整里面的 getattr 兜底。

## 它怎么插进系统(给 reviewer)

不需要新写任何"连接代码":`deep_literature` 复用了网关本来就提供给每个工具的每次运行上下文
(`ctx.tunnel_port` / `ctx.model`),和 `scrna_pack`、`literature_search` 完全一样。
任务一旦启动、Qwen 隧道一旦建立,工具就会自动连上模型。
