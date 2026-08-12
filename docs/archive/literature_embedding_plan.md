# 文献库 + Embedding 方案(已定:two-tier references,无本地 embedding)

> **状态:已定方案,不再是"清单选型"。** 取代旧版"给 PaperQA 选检索源 + 选本地 embedding"的讨论清单。
> 目的:给生成报告加 reference,并(下一步)前置文献检索让大模型在写作时就用上文献。
> 约束不变:**私有数据集不出 UCI;只有公开文献检索允许联网。**(关键:出去的只有公开的 *研究问题*,
> 不是数据集——已与 JinLi 确认 query 不含敏感数据。)

---

## 0. 一句话结论

- **检索 = 两级**:Tier 1 远端一站式 RAG(**FutureHouse / Edison Scientific,agent Crow**)→ Tier 2 **Europe PMC 关键词** 兜底。
- **Embedding + 向量库 = 不自建**:Tier 1 由 provider 全包;Tier 2 是关键词检索、根本不用 embedding。旧版"必须本地 embedding"的约束**作废**(理由见 §3)。
- **已落地代码**:`src/bioagent/tools/literature_references.py` 填报告的 `## References` 槽;fallback 记进**技术报告**,学术手稿照常渲染;绝不编引用。
- **下一步(最好做)**:把检索**前置**,让大模型写 Introduction/Discussion 时自己查文献(Mode B,见 §4)。

---

## 1. 检索:最终组合(从旧清单里收敛而来)

| 级别 | 选定 | 角色 | 全文? | embedding | 状态 |
|---|---|---|---|---|---|
| **Tier 1(主)** | **FutureHouse / Edison Scientific 平台 · agent Crow**(PaperQA2 内核) | 远端一站式 RAG:搜→嵌入→向量检索→合成,返回**带引用的答案** | ✅ provider 侧 | **provider 的** | API 真实存在(`edison-client`,api-key);**待设 key + 确认数据政策** |
| **Tier 2(兜底)** | **Europe PMC REST**(已在用 `literature_search`) | 远端挂了但有外网时,关键词检索真 DOI/PMID | OA 部分 | **无** | ✅ 已落地 |

**为什么是 Crow 而不是旧清单 E 区那些(Consensus/Elicit/SciSpace/Undermind):** 那几家**没有公开开发者 API**,接不进 agent(旧清单 §E 自己也这么写)。而 FutureHouse/Edison 平台**有** Python 客户端(`edison-client`,前身 `futurehouse-client`)+ api-key,`run_tasks_until_done` 提交任务,Crow 直接基于 PaperQA2——和我们 in-loop 的 `deep_literature` 同源,引用风格一致。这是清单里**唯一**真正"一站式 RAG + 有 API"的远端方案(旧清单只列了同家的 paper-scraper 抓取工具,低估了它们的托管平台,这里补上)。

**Tier 2 为什么是 Europe PMC 而不是 CORE/S2/OpenAlex:** 兜底只需要"轻量、免 key、生物医学、给真 DOI"——Europe PMC 全中,且代码已就绪。CORE(OA 全文聚合最大)可作为**可选的第三级**扩展全文覆盖,但非必需。完全离线场景**不考虑**(UCI 机器断网时两个服务都连不上)。

---

## 2. 接入方式(代码现状)

`literature_references.py` 的 Tier 1 有两种接法,env 门控,未配置就自动降级:

1. **原生客户端**:`pip install edison-client` + api-key。⚠️ **OPEN ITEM**:拿到真实 key 后先确认客户端 API 和返回结构,再固化原生 adapter(沿用 `paperqa_search` 的"上服务器再确认"做法)。
2. **通用 REST escape hatch(当前已接的)**:任何 OpenAI 风格端点放到 `BIOAGENT_LITERATURE_REMOTE_URL`(+ 可选 `BIOAGENT_LITERATURE_REMOTE_KEY`)。例如团队自己在 `edison-client` 前面套一个 10 行 REST wrapper——这条路现在就能用。

**TODO(给 MaziYao / 文献线):**
- [ ] 申请 FutureHouse/Edison API key,确认**数据留存 / 是否用于训练**的政策(合规前提)。
- [ ] 二选一:套 REST wrapper 走通用端点(快),或拿 key 验证后写原生 adapter(稳)。
- [ ] 设 `BIOAGENT_LITERATURE_REMOTE_URL`(或 key),主路自动从 Europe PMC 切到 Crow。

---

## 3. Embedding:为什么"本地 embedding"约束作废

旧版 §2 写"embedding **必须本地**(隐私核心)"。**前提变了:**

- 那条约束是为"自托管 PaperQA、把论文块向量化"服务的——本地是为了**别把要嵌入的文本发出去**。
- 新方案里 Tier 1 嵌入的是**公开论文**、query 是**公开研究问题**,这些本来就允许联网检索;Tier 2 干脆**不嵌入**(关键词)。
- 所以"为隐私而本地 embedding"**不再适用**。我们一行 embedding 代码都不维护(MedCPT / BMRetriever / bge-large 的选型问题**直接消失**)。

> 注:仓库里另有独立工具 `deep_literature`(`paperqa_search.py`),Scientist 过程中自调,**仍用**本地 `st-` embedding。**这条按用户要求保持不动**,与本 references 模块无关。日后若要统一,可让它也改走 Crow——属于另一个改动。

---

## 4. 下一步:前置检索,让大模型写作时就用上文献(Mode B)

**现状是 Mode A(末端引用器):** 手稿先只凭分析结果写完,最后才往 `## References` 填引用。论断本身没被文献 ground。

**目标 Mode B(用户想要的):** 把检索**提前**,只对**依赖文献的两节**做:

```
写 Introduction / Discussion 之前:
  1. 把关键 findings 拆成几个聚焦问题
  2. 每个问题调 Crow(远端)/ Europe PMC(兜底)→ 拿到带引用的证据包
  3. 把证据包喂进这两节的写作 prompt
  4. 大模型基于真实证据下笔,inline 引用
  5. ## References 成为"实际引用过什么"的副产品 —— 自动一致
```

- Results / Methods **保持无文献**(来自数据,不需要)。
- fallback 时拿不到合成证据、只有论文列表,这两节自动退化成"LLM 自行发挥 + 摘要弱 grounding",降级照样记进技术报告——与 Tier 设计自洽。
- 落点:`gateway/app.py` 的 `_REPORT_WRITER_SYSTEM` 编排(retrieve-before-write),是个**报告流程改造**,尚未实现。

---

## 5. 成本与限流(cost 是关键决策因素)

### Tier 2 — Europe PMC:实质零成本

- **免费、无需 key、不碰付费墙。** 我们只取 metadata + DOI/PMID 做引用,不拉闭源全文,所以永远撞不到 paywall。
- **限流是 EBI "合理使用"软限制(偶发 429),不是收费。** 我们每份报告几次查询,离触发限流差几个数量级。代码已带描述性 `User-Agent`;批量时再加联系邮箱 + 限速即可。
- ⟶ 兜底挂了也不花钱,不会因远端账单瘫痪。

### Tier 1 — Crow(Edison):credit 计费

- 模型:**买 credits → 建 API key → 每次调用扣 credits**。有"generous free tier",power user 付费换更高 rate limit / 更多额度。**Crow(快问)比 Falcon(深度综述)便宜。**
- ⚠️ **确切 $/credit、单次 Crow 扣多少、免费额度上限 = 未确认**(`edisonscientific.com/pricing` 与 FAQ 现 404,docs quickstart 不写价)。**必须登录 `platform.edisonscientific.com` 或找它们要 live 报价。不要凭猜定预算。**

### 我们的用量天然小 + 四个降本杠杆

用量:纯填 References ≈ **每份报告 1 次 Crow**;Mode B ≈ **每份报告 3–8 次**。报告非高频操作,**即使付费单份成本也低**。

1. **只用 Crow,不用 Falcon** 做引用(Falcon 深度综述对"找引用"是浪费)。
2. **Europe PMC 先行 / Crow 按需**:便宜元数据先走免费的 Europe PMC,只有真要"带证据的合成答案"(Mode B 关键段)才花 credits。**最大杠杆。**
3. **本地缓存去重**:查过的"问题→答案/引用"缓存到本地,避免**同一问题重复扣 credits**。这才是"本地组件"真正有用的地方 —— 省的是**钱**,不是离线(区别于早先讨论的 offline-grep)。键 = normalize 后的 question;value = `gather_references` 结果;可按运行/工作区或全局持久化。
4. **每份报告的 Crow 调用上限**(Mode B 拆问题 ≤N 个),给单份成本封顶。

### TODO(文献线)
- [ ] 登录 Edison 平台拿**确切定价 + 免费额度**,回填本节。
- [ ] 把"Europe PMC 先行 / Crow 按需 + 本地缓存"定为默认省钱策略(而非无脑每次打 Crow)。
- [ ] 决定缓存层是否实现、放哪(可由 Yijun 核心线实现,接口同 `gather_references`)。

## 来源
- FutureHouse/Edison 平台 + 客户端:https://www.futurehouse.org/research-announcements/launching-futurehouse-platform-ai-agents ；https://docs.edisonscientific.com/edison-client
- PaperQA2(Crow 内核):https://github.com/Future-House/paper-qa ；https://arxiv.org/abs/2312.07559
- Europe PMC / 科研 API 综述:https://intuitionlabs.ai/articles/research-paper-apis-scientific-literature
- 多源检索 MCP(备查):https://github.com/benedict2310/Scientific-Papers-MCP
