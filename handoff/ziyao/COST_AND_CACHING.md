# 文献检索 · 成本与缓存汇报(转交 Ziyao Ma)

> 2026-06-30 更新:本文记录的是旧的 `gather_references()` / remote-Crow fallback 方案。当前实现已改为
> **不在最终报告阶段做隐藏检索**:Europe PMC 只通过显式的 `literature_search` step 调用;最终
> `literature_references.py` 只格式化/插入本轮 accepted citations。因此本文的缓存建议已过时,仅作历史背景。

> 这是一份可直接转发的汇报。背景:报告的 References 由两级检索产生 —— Tier 1 远端
> **Edison/FutureHouse Crow**(主路)→ Tier 2 **Europe PMC**(兜底)。本文只讲**成本、限流、
> 以及怎么省钱**。完整方案见 `docs/archive/literature_embedding_plan.md`,模块见
> `src/bioagent/tools/literature_references.py`。
>
> 状态:references 模块已落地 `main`;远端 Tier-1 当前**休眠**(env 未配),实际跑的是 Europe PMC。

---

## 一、两级检索的钱怎么花

### Tier 2 — Europe PMC:实质零成本(放心)

- **免费、无需 API key、不碰付费墙。** 我们只取 metadata + DOI/PMID 拼引用,**不下载闭源全文**,
  所以永远撞不到 paywall(付费墙只存在于 Nature/Cell 这类闭源全文,我们不碰)。
- **限流 = EBI「合理使用」软限制**(高并发偶发 HTTP 429),**不是收费**。我们每份报告几次查询,
  离触发限流差好几个数量级。
- 代码已带描述性 `User-Agent`;若将来批量化,补一个联系邮箱 + 限速即可。
- ⟶ **兜底层不会产生任何账单,远端出问题也不会因为钱而瘫。**

### Tier 1 — Crow(Edison Scientific):credit 计费

- 计费模型(已核实):**先买 credits → 建 API key → 每次 agent 调用扣 credits**。
- 有「**generous free tier**」(轻量用免费),power user 付费换更高 rate limit / 更多额度。
- **Crow(快速问答 agent)比 Falcon(深度综述 agent)便宜。** 我们做引用用 **Crow** 就够,Falcon 不用。
- ⚠️ **未确认项(重要):确切 $/credit、单次 Crow 调用扣多少 credits、免费额度上限。**
  `edisonscientific.com/pricing` 和 FAQ 页当前 404,`docs.edisonscientific.com` 的 quickstart 也不写价。
  **→ 需要登录 `platform.edisonscientific.com` 拿 live 报价,或直接联系 Edison。预算前别凭猜。**

---

## 二、好消息:我们的用量天然很小

| 场景 | 每份报告的 Crow 调用 |
|---|---|
| 纯填 References(现状 Mode A) | ≈ **1 次** |
| 写作时查文献(Mode B,未实现) | ≈ **3–8 次** |

报告不是一天跑几千次的东西,所以**即使付费,单份报告的 Crow 成本也很低**。真正要防的是"无脑每次都打
Crow、且重复查同样的问题"。

---

## 三、四个降本杠杆(建议定为默认策略)

1. **只用 Crow,不用 Falcon** —— Falcon 的深度综述成本对"找引用"是浪费。
2. **Europe PMC 先行 / Crow 按需** —— 便宜的元数据先走免费的 Europe PMC,只有真需要"带证据的合成答案"
   (Mode B 的关键段落)才花 Crow credits。**这是最大的一根杠杆。**
3. **本地缓存去重** —— 把查过的「问题 → 答案/引用」缓存到本地,避免**同一个问题重复扣 credits**。
   - 这才是"本地组件"真正有用的地方:省的是**钱**,不是离线能力(区别于早先讨论的 offline-grep)。
   - 实现:key = normalize 后的 question;value = `gather_references()` 的返回;按工作区或全局持久化。
   - 接口与 `gather_references` 一致,可由 Yijun 核心线实现,文献线只需确认要不要 + 缓存放哪。
4. **每份报告封顶 Crow 调用数**(Mode B 拆问题 ≤N 个),给单份成本设上限。

---

## 四、给文献线的待办(Ziyao Ma)

- [ ] **拿确切定价**:登录 `platform.edisonscientific.com`,确认 $/credit、单次 Crow/Falcon 扣费、免费额度,回填本文 + `docs/archive/literature_embedding_plan.md` §5。
- [ ] **确认数据政策**:query 是否被留存 / 用于训练(合规前提;我们只发公开研究问题,但 provider 侧要核实)。
- [ ] **拍板省钱策略**:是否采用"Europe PMC 先行 / Crow 按需 + 本地缓存"为默认。
- [ ] **缓存层**:是否实现、放哪(可由 Yijun 线实现,接口同 `gather_references`)。
- [ ] 在拿到定价前:维持 Europe PMC 跑兜底(**零成本**),系统照常出引用。

---

## 五、一句话给老板/对外口径

> "加引用的兜底走 Europe PMC,**免费、无账单风险**;主路用 Edison/FutureHouse 的 Crow agent(PaperQA2 内核),
> **按 credit 计费但用量小、且有免费额度**;通过'Crow 按需 + 本地缓存去重'把成本压到很低。确切单价待与 Edison
> 确认后回填。"

— 由 Yijun 核心线整理(2026-06-28),供文献线 Ziyao Ma 汇报使用。
