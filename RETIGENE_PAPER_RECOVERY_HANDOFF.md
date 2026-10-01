# RetiGene 论文恢复交接文档

更新时间：2026-07-13（America/Los_Angeles）

仓库：`/Users/maziyao/Desktop/summer intern/code/BioAgentPrototype`


## 0ac. 续跑点（2026-07-15，UC Library Search 全部跑完）

- 实时：**已选 1741 / 待恢复 8**。原 16 篇经图书馆共恢复 **8 篇**：Science ×3（ProQuest/JSTOR）、Diabetes Care 9353617（ProQuest）、Ophthalmic Genet 11135490（T&F）、Retin Cases 32150116 + 33973556（Ovid，SUBSCRIBED；33973556 首次报 Ovid `E3` 后重试即成功）、Healio OSLI Retina 35148219（ProQuest）。
- **剩余 8 篇均确认无 UCI 全文、走 ILL**（清单见 `RetiGene_ILL_remaining_8.xlsx`）：41202168（Retin Cases 2025 预出版，Ovid Check Access，等正式出版）、7124876/8190471/8302562（老 Ophthalmology/AmJOphth，Primo 仅引文）、11472682（Med Clin 西语）、27486893（Healio JPOS，EBSCO 链接跳出版社、ProQuest 无收录）、26043506（Genet Couns 无 DOI）、37471664（Altern Ther，仅 CINAHL/Scopus 索引无全文）。
- 经验补充：Healio 两本刊 UCI 覆盖不同——**OSLI Retina 在 ProQuest 有全文，JPOS 只有 EBSCO A-to-Z 跳出版社（拿不到）**。Ovid `License Service Failure E3` 多为临时，隔一会儿重试即可。

## 0ab. 续跑点（2026-07-15，经 UC Library Search 又恢复 6 篇）

- 实时：**已选 1739 / 待恢复 10**。导师提示"剩下的从 UCI 图书馆能拿到"属实：**UC Library Search（Primo 发现层，正确 host 是 `uci.primo.exlibrisgroup.com`）会去 UCI 订阅的聚合库找全文**（ProQuest / JSTOR / EBSCO / T&F / Ovid 等），很多出版社直连锁着的老文其实能下。
- **本轮经图书馆恢复 6 篇**：Science ×3（2349482, 3201231 → ProQuest；8202715 → JSTOR）、Diabetes Care 9353617（ProQuest）、Ophthalmic Genet 11135490（Taylor & Francis，**旧刊其实有订阅**）、Retin Cases 32150116（Ovid，**SUBSCRIBED**，走 Primo 的 oce.ovid.com 入口，与 doi.org 直连锁不同）。
- **有效方法**：Primo 按 **DOI** 精确搜（题名易撞相似新文）→ 结果行有 `Get PDF`/`Download PDF` 就直接点；只有 `Available Online` 时**点标题进记录详情看 "View Online" 真实来源**（搜索结果的 Available Online 快捷链接有时错误指向 LexisNexis 登录页）。ProQuest/JSTOR 首次需过一次 cookie+条款（已授权，本会话记住）；ProQuest 老文多为扫描版（正文可提取文本少但页面完整）。
- **剩余 10 篇状态**：
  - `33973556`（Retin Cases 2023）：UCI 有 Ovid 权限，但该篇 Ovid 后端持续报 `License Service Failure (Code: E3)`——非权限问题，**稍后重试**或走 ILL。
  - `41202168`（Retin Cases 2025）：**Publish-Ahead-of-Print**，Ovid 显示 Check Access，尚未进订阅窗口 → 等正式出版或 ILL。
  - `7124876`/`8190471`/`8302562`（Am J Ophthalmol / Ophthalmology，ClinicalKey）：Primo 仅有引文记录，无 UCI 全文持有（Available Online 错误指向 Lexis）→ **ILL**。
  - `11472682`（Med Clin，西语）：仅 ILL 选项 → **ILL**。
  - `27486893`/`35148219`（Healio）：EBSCO A-to-Z 链接跳出版社存档首页、需另登录，无干净全文 → **ILL/人工**。
  - `26043506`（Genet Couns）、`37471664`（Altern Ther）：无 DOI 冷门刊 → **ILL**。

## 0aa. 续跑点（2026-07-14 晚，j-式 Elsevier 队列已清空）

- 实时：**已选 1733 / 待恢复 16**。本轮把 `_resume_jstyle.csv` 里 20 篇 j-式 Elsevier 全部下齐并验证通过（pypdf header+页数全 OK），`_resume_jstyle.csv` 已清空为仅表头。
- **VPN 全隧道下 Ophthalmology / Am J Ophthalmol / oret / jaapos / preteyeres / exer / ymgme / jgg / gene 等此前以为「ClinicalKey 未订阅」的刊其实都能下**（页面显示 Full text access + View PDF）。校验：`_ingest.py` 的 `BASE` 已改为按脚本自身位置推导（跨会话稳定），`newest_pdf()` 只认 `*.pdf`（避免误抓 csv）。
- **剩余 16 篇全部需 ILL / 人工**（j-式队列已无可自动下的）：Science 3（2349482, 3201231, 8202715）；ClinicalKey 老式 Elsevier DOI 3（7124876, 8190471, 8302562）；LWW/Retinal Cases 3（32150116, 33973556, 41202168）；Healio 2（27486893, 35148219）；Diabetes Care 9353617（按次付费）；Med Clin 11472682；Ophthalmic Genet 老 Swets 11135490；Genet Couns 26043506（无 DOI）；Altern Ther 37471664（无 DOI）。

## 0a. 续跑点（2026-07-14，VPN re-sweep 未完）

- 实时：**已选 1713 / 待恢复 36**（本轮 re-sweep 已补 55 篇；发现旧"91篇拿不到"名单严重高估，VPN 全隧道下绝大多数 Elsevier 眼科/中端刊其实能下）。
- **续跑方法**：VPN(UCIFull) 保持连接、Chrome 下载目录与"下载PDF不打开"设置别动。逐条读 `output/retigene_papers/journal_priority/_incoming_downloads/_resume_jstyle.csv`（还剩 20 篇可下的 j-式 Elsevier）：`https://doi.org/<DOI>` → 等4s → 点顶部 `View PDF`（PDF Options 里那个，不是内嵌阅读器/不是 View full text）→ `python3 _incoming_downloads/_ingest.py <pmid> elsevier`。每篇点击前先关掉上一篇残留的标签页；下载偶发不触发就重试一次；连续快速下~35篇后 crasolve 反爬会拦截自动点击，暂停约60-90s或改由用户手点即可恢复。
- **真正拿不到的少数（走 ILL）**：ClinicalKey-only 的 7124876、8190471、8302562；Med Clin 11472682（SD明示未订阅）；Science 3（2349482,3201231,8202715）；Healio 2（27486893,35148219）；LWW 3（32150116,33973556,41202168，UCI不在Ovid列表）；Diabetes 9353617（按次付费）；Ophthalmic Genet 11135490（旧T&F未订阅）；Genet Couns 26043506、Altern Ther 37471664（冷门刊）；gim 2026 在印 41904678。

## 0. 最新会话进度（2026-07-13 晚，UCI 浏览器 + VPN 批次）

- 实时计数：**总 1749 / 已选 1658 / 待恢复 91**（本会话新恢复 **80** 篇，全部 `selected` 且已验证，1658 个文件全部存在、无损坏）。含 Liebert 1（Cloudflare 需用户手点一次后 `/doi/pdf/<DOI>?download=true` 直连）。
- 本会话来源分布：Wiley 17、Elsevier(ScienceDirect) 20、BMJ 12、Neurology 8、IOVS/ARVO 6、Genetics in Medicine 6、SAGE 5、Springer 2、Taylor&Francis 1、ACS 1、viamedica 1。（ScienceDirect 含 JID/Gastro/AJKD/BBRC/Genomics/Can J Ophthalmol/J Pediatr/JBC 等单篇，JBC 是从 PubMed 的「Elsevier Science」全文链接找到 SD 上的 PII。）
- **IOVS/ARVO 方法（VPN 下无 Cloudflare）**：PubMed 文章页 → 「Silverchair」链接拿到 `iovs.arvojournals.org/article.aspx?volume=V&issue=I&page=P` → 打开后点工具栏「PDF」即自动下载。
- **ScienceDirect 点击下载的小技巧**：`View PDF` 点击偶发不下载时，**先关掉上一次残留的 crasolve 标签页再点一次**即可成功（残留标签会干扰下一次下载）。

### 剩余 92 篇：基本是走不通/需人工的
- **约 74 篇 Elsevier 中端刊 / Ophthalmology / Am J Ophthalmol（ClinicalKey）**：UCI 未订阅，SD 上只有 Purchase/Access，无解。
- **Liebert(10.1089) 1（PMID 7832988）+ Healio(10.3928) 2**：Liebert 有 Cloudflare「Verify you are human」（像 SAGE，需用户手点一次即可）；Healio `/doi/pdf/` 只出内嵌阅读器、访问权不明，需人工另存。
- **LWW/Retinal Cases(10.1097) 3**：ovid.com 显示 "Check Access" 锁，VPN 下也未自动授权（需 Ovid 机构登录）。
- **Science(10.1126) 3**：旧存档不在 UCI 订阅内，锁。
- **Diabetes Care(10.2337) 1**：pay-per-view，无解。
- **Ophthalmic Genet 老 Swets DOI(10.1076) 1（PMID 11135490）**：DOI 已失效，PubMed 也无免费/出版社全文链接，需另查。
- **Genet Couns 1、Altern Ther Health Med 1**：冷门刊，需单独找。

（本会话已把 JBC 1 篇[从 PubMed→SD]、IOVS/ARVO 6 篇[PubMed→arvojournals 点 PDF] 全部补齐。）
- 又几个直连方法：**Springer** `link.springer.com/content/pdf/<DOI>.pdf`；**Taylor&Francis** `tandfonline.com/doi/pdf/<DOI>?download=true`；**SAGE** `journals.sagepub.com/doi/pdf/<DOI>?download=true`（有 Cloudflare，需用户手点一次「Verify you are human」，之后同会话其余可自动）；**viamedica(OJS)** doi.org→找「Download PDF file」真实 href `.../article/download/<id>/<galley>` 直连。
- 注意：VPN 下 ScienceDirect 的 `View PDF` 点击/`pdfft` 直连本会话后段变得不稳定（crasolve 反爬偶发拦截、返回空白不下载），剩下的 SD-Elsevier 单篇（Gastro 24726755、BBRC 8240356、Genomics 12160730 等，均确认 UCI 有全文）需要重试或换时间再点。
- 两个阶段：先用 **Elsevier SSO 登录**拿 ScienceDirect + Genetics in Medicine；后来用户连上 **UCI VPN（全隧道 UCIFull）**，Wiley/BMJ/Neurology/ACS 用「校园 IP + 直连 PDF URL」全自动下载。

### VPN 阶段已验证的直连 PDF 方法（连 UCIFull 全隧道后，配合 Chrome「下载 PDF 而非打开」）
- **Wiley**：`https://onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true`（FASEB 用 `faseb.` 子域）。
- **BMJ (J Med Genet)**：doi.org 落地 `jmg.bmj.com/content/<vol>/<iss>/<page>` → PDF = `https://jmg.bmj.com/content/jmedgenet/<vol>/<iss>/<page>.full.pdf`；BMJ Case Rep = `casereports.bmj.com/content/bmjcr/<vol>/<iss>/<page>.full.pdf`。
- **Neurology**：`https://www.neurology.org/doi/pdfdirect/<DOI>?download=true`（`/doi/pdf/` 只出内嵌阅读器，要用 `pdfdirect`）。
- **ACS**：`https://pubs.acs.org/doi/pdf/<DOI>?download=true`。

### 仍待恢复 116 篇的情况
- **付费墙 / 未订阅的 Elsevier（约 74）**：ejmg/exer/gene/ymgme/yexcr/jgg/preteyeres 等中端刊，及 Ophthalmology/Am J Ophthalmol(ClinicalKey)。这批 SD 上只有 Purchase/Access，基本走不通。
- **需人工过验证码**：SAGE(`journals.sagepub.com`) 4 篇有 Cloudflare「Verify you are human」，要用户手点。
- **未订阅存档**：Science(`science.org`) 3 篇旧存档显示锁（UCI Science 订阅不含该年代）。
- **零散单篇待逐个试**：LWW/Retina Cases(10.1097) 3、Liebert(10.1089) 1、JID(10.1038) 2、Diabetes Care(10.2337) 1、Healio(10.3928) 2、tandfonline Ophthalmic Genet(10.1076/10.3109) 2、Can J Ophthalmol(10.3129) 1、10.1006/10.1053/10.1074 等 Elsevier-on-SD 若干（Genomics 12160730 已确认 UCI 有全文，只是点击下载偶发失败，可重试）。
- **无 DOI（8）**：IOVS/ARVO 6（`iovs.arvojournals.org`，部分开放获取）、Genet Couns 1、Altern Ther Health Med 1。

### 下一步建议
- SAGE：让用户手点一次 Cloudflare 验证框后，`https://journals.sagepub.com/doi/pdf/<DOI>?download=true` 应可下。
- IOVS：ARVO 旧文多为 OA，可单独试 `iovs.arvojournals.org` 的 PDF。
- Elsevier-on-SD 单篇（10.1006/10.1053/10.1074 等）：doi.org → ScienceDirect → 点 `View PDF`（偶发不触发下载时重试一次）。

---

## 0-old. 上一小节（Elsevier SSO 阶段，仅供参考）

- 方法：Claude-in-Chrome 扩展 + 用户完成一次 UCI(University of California Irvine University Libraries) 的 Elsevier SSO 登录，登录态在整个会话保持。
- 关键设置（用户已在 Chrome 中打开）：默认下载目录设为
  `output/retigene_papers/journal_priority/_incoming_downloads/`，
  并开启「下载 PDF 而不是在 Chrome 打开」(`chrome://settings/content/pdfDocuments`)、关闭「每次询问保存位置」。
- 入库脚本：`output/retigene_papers/journal_priority/_incoming_downloads/_ingest.py <pmid> <tag>`
  （取该目录最新 PDF → 移入 `papers_priority/` → pypdf 验证页数/正文 → 更新 manifest `selected_status=selected` 与 needs 清单）。

### 本会话已恢复的 17 篇
- ScienceDirect（UCI 订阅的老牌刊，直接 `View PDF`）：1678337, 1900003, 2039493, 2511845, 7553855, 7664335, 8253776, 8757578, 9390563, 9635427, 10371079
- Genetics in Medicine（`gimjournal.org`，直接 `/article/<PII>/pdf` 自动下载）：35331648, 35486108, 35986737, 37057675, 40119724, 41045073

### 已验证有效的下载方法
1. Elsevier 统一走 **ScienceDirect by PII**：`https://www.sciencedirect.com/science/article/pii/<PII>`。
   - 老式 DOI（`10.1016/0006-...`、`10.1016/s0161-...`）的 PII = DOI 后缀去标点转大写。
   - `j.` 式 DOI 需先 `https://doi.org/<DOI>` 解析拿到落地 URL 里的 PII。
   - 若页面出现 `View PDF`（绿色 Full text access）= UCI 有订阅；点它会新开一个标签并自动下载。
   - **注意反爬**：`location.href` 直接跳 `/pdf?` 会被 crasolve 反爬判为自动化、返回空白页；必须用「点击 `View PDF` 链接」这种可信手势（扩展 `computer left_click` 或用户手点都可）。
2. Genetics in Medicine：`https://www.gimjournal.org/article/<PII>/pdf` 直接导航即自动下载（PII 从 doi.org 落地页取）。

### 访问情况判定（UCI 当前 Elsevier 订阅）
- **可下载**：Cell、J Biol Chem、BBRC、Comp Biochem Physiol、J Pediatr(老)、J Neurol Sci、Genetics in Medicine。
- **付费墙 / 未订阅（SD 上只有 Access/Purchase，跳过）**：Eur J Med Genet(ejmg)、Exp Eye Res(exer)、Gene、Mol Genet Metab(ymgme)、Exp Cell Res(yexcr)、J Genet Genomics(jgg)、Prog Retin Eye Res(preteyeres)、BBA-Lipids 等中端 Elsevier 刊。
- **ClinicalKey/自有平台，SD 无干净 PDF（跳过）**：Ophthalmology(j.ophtha/S0161-6420)、Am J Ophthalmol(j.ajo/S0002-9394、ajo.com)、JAAPOS、Ophthalmology Retina(oret)、J Pediatr(新，jpeds.com)。

### 下一步（需要用户参与）
- 剩余 154 篇里，非 Elsevier 的 **Wiley(17)、BMJ(12)、Neurology(8)、SAGE(4)、Science(3)、LWW(3)** 等各自需要**独立的 UCI 机构登录**（Elsevier 的登录态不通用）。Wiley 实测 `onlinelibrary.wiley.com/doi/pdfdirect/<DOI>` 现在是 `Login/Register` 未登录态。建议下个会话让用户走 UCI 图书馆 EZproxy 或各出版社「Institutional login → UCI」。
- IOVS(6，无 DOI，ARVO) 多为开放获取，可单独尝试。
- 待恢复清单实时见 `needs_journal_or_uci_access.csv`；分类队列见 `_incoming_downloads/_queue_*.csv`。

## 1. 当前准确状态

- 总论文数：**1749**
- 已确认可用：**1578**
- 仍待恢复：**171**
- `papers_priority/` 中有 **1633** 个 PDF，但不能用文件数判断成功数，因为损坏旧文件被保留用于诊断。
- 唯一可信的成功条件是 manifest 中 `selected_status == selected`。

主要文件：

- 主清单：`output/retigene_papers/journal_priority/retigene_priority_manifest.csv`
- 当前待恢复清单：`output/retigene_papers/journal_priority/needs_journal_or_uci_access.csv`
- PDF 目录：`output/retigene_papers/journal_priority/papers_priority/`
- 损坏文件复核结果：`output/retigene_papers/journal_priority/invalid_selected_reconciliation_summary.json`
- 最近一次完整性扫描：`output/retigene_papers/journal_priority/corpus_validation_summary.json`

注意：`corpus_validation_summary.json` 是在撤回损坏文件前生成的，里面的 selected 数量已经过时；损坏文件列表仍然有效。当前数量应从 manifest 实时计算。

## 2. 为什么原先说 222 篇失败，现在是 171 篇

原始清单显示 222 篇未下载。之后对整个库做了 PDF 解析检查，发现旧清单还把 **54 个损坏或截断的 PDF** 误标成成功；另有 1 个“缺失”只是路径写短，实际文件完整 10 页，已修正路径。

因此真实起点是：

- 原始未下载：222
- 旧的假成功：54
- 真实待恢复：276

目前已从这 276 项中补回 **105 篇**，所以剩余 **171 篇**。

54 个损坏旧文件中，已有 **23 篇**重新恢复（6 篇来自开放 API，17 篇来自 PMC 完整正文）；还有 **31 篇**未恢复。损坏原文件没有删除，但已经从 selected 集合撤回，PaperQA 不应再读取它们。

## 3. 当前浏览器状态

用户已经手动完成 ScienceDirect 的机器人验证码。

当前页面：

`https://www.sciencedirect.com/science/article/abs/pii/030096299190030G?via%3Dihub`

文章：`Early postnatal development of peptide hydrolysis in chicks and guinea pigs`

页面现在显示：

- `Access through your organization`
- `Purchase PDF`

说明验证码已通过，但 **尚未获得机构全文权限**。上一会话准备点击机构入口时被用户主动停止，所以没有进入 UCI 登录页。

新会话应重新加载 browser skill，并连接当前 in-app browser 标签页。不要依赖旧 Node REPL 变量，它们不会可靠地跨新会话使用。

## 4. 接下来最优先做什么

1. 在当前 ScienceDirect 页面点击 `Access through your organization`。
2. 让用户手动选择 University of California, Irvine / UCI，并完成账号、密码和可能的双重验证。
3. 不要读取、记录或代填用户密码和验证码。
4. 登录后回到当前文章，确认页面出现 `View PDF`、`Download PDF` 或可访问的 `pdfft` 链接。
5. 先下载当前这 1 篇并用 `pypdf`/`pdfinfo` 验证，再批量处理剩余 Elsevier 论文。
6. 完成 Elsevier 后，再处理 Neurology 的 8 篇机构访问文章。
7. 最后处理零散出版社、IOVS 和唯一剩余的 PMC PDF。

不要绕过 CAPTCHA、登录或付费墙。可以使用用户正常拥有的 UCI 机构权限。

## 5. 剩余 171 篇的主要分布

按 DOI 前缀：

| 数量 | DOI 前缀 | 主要来源 |
|---:|---|---|
| 96 | `10.1016` | Elsevier / ScienceDirect |
| 12 | `10.1136` | BMJ |
| 11 | `10.1111` | Wiley |
| 8 | 无 DOI | 6 篇 IOVS + 2 篇难找文章 |
| 8 | `10.1212` | Neurology |
| 5 | `10.1002` | Wiley |
| 4 | `10.1177` | SAGE |
| 3 | `10.1126` | Science |
| 3 | `10.1097` | LWW |
| 21 | 其他前缀 | Springer、Healio、ACS 等零散出版社 |

数量最多的期刊：Ophthalmology 20、J Med Genet 11、Am J Ophthalmol 10、Clin Genet 9、Neurology 8、Genet Med 7。

## 6. 已经使用并成功的方法

### PMC / NCBI

- PMC 完整 HTML 抓取并重排为 PDF：当前 **39 篇**
- PMC 扫描页重建 PDF：**17 篇**
- NCBI Bookshelf / GeneReviews 官方 PDF：**5 篇**
- 新增脚本：`scripts/capture_remaining_pmc_html.py`
- 导入脚本：`scripts/import_pmc_browser_captures.py`

最新一轮从 18 个剩余 PMCID 中成功抓到 17 篇完整正文。唯一未完成的是：

- PMID `15286153`
- PMCID `PMC1735855`
- 标题 `Mutations of ESPN cause autosomal recessive deafness and vestibular dysfunction`
- 官方 PDF：`https://pmc.ncbi.nlm.nih.gov/articles/PMC1735855/pdf/v041p00591.pdf`

命令行访问该 PDF 会返回 NCBI 的 `Preparing to download` proof-of-work 页面。需要在正常浏览器中打开，让官方页面完成验证后下载；不要绕过其验证。

### Europe PMC

- Europe PMC 原始 PDF：**15 篇**
- 完整 HTML 重排也走 `import_pmc_browser_captures.py`
- PDF URL 导入脚本：`scripts/import_europepmc_pdf_urls.py`

### 开放数据库和机构仓储

- Semantic Scholar 开放 PDF：**4 篇**
- OpenAlex 开放 PDF：**3 篇**
- 其他合法机构仓储：**15 篇**
- API 恢复脚本：`scripts/recover_retigene_open_access_apis.py`
- 已知仓储导入：`scripts/import_known_repository_pdfs.py`

最近对 194 个待恢复项重跑 OpenAlex + Semantic Scholar，补回 6 篇损坏旧文件。

### Internet Archive

- Molecular Vision 历史原始 PDF：**5 篇**
- 脚本：`scripts/recover_molecular_vision_wayback.py`

### 出版社完整 HTML

- Wiley 开放全文 HTML：1 篇（PHB1，PMID `42067999`）
- Wiley 完整文章 HTML：1 篇（AP5Z1，PMID `33543803`）
- 都通过通用化后的 `scripts/import_pmc_browser_captures.py` 转成可检索 PDF。

## 7. 已经尝试但没有解决的方法

- `scripts/download_journal_pdfs_with_uci_vpn.py` 对 43 篇非 Elsevier DOI 重试：0 篇更新，主要是 HTTP 错误或返回 HTML 而不是 PDF。
- ScienceDirect 自动访问此前遇到 CAPTCHA；用户现已手动通过，但仍需 UCI 机构登录。
- Neurology 页面显示 `Get Access`，也需要机构登录。
- IOVS 老论文页面标记 Free，但 PDF 请求被 Cloudflare 拦截；不要绕过。
- SAGE 的公开 PDF 搜索结果在命令行和浏览器都触发 403/Cloudflare。
- CiteSeerX 的 PMID `11527955` PDF 链接已重定向到失效的 Internet Archive 快照，没有可用文件。
- Unpaywall/OpenAlex/Semantic Scholar 对大多数剩余项没有公开 PDF URL。

## 8. 关键脚本说明

| 脚本 | 用途 |
|---|---|
| `scripts/reconcile_invalid_selected_pdfs.py` | 把 54 个不可读旧 PDF 从 selected 撤回，并修复 1 个路径 |
| `scripts/recover_retigene_open_access_apis.py` | 查询 OpenAlex、Semantic Scholar，验证并导入合法公开 PDF |
| `scripts/capture_remaining_pmc_html.py` | 从 NCBI PMC 抓完整 `<article>` HTML |
| `scripts/import_pmc_browser_captures.py` | 将 PMC 或出版社完整 HTML/扫描页转换为 PDF，并更新 manifest |
| `scripts/import_europepmc_pdf_urls.py` | 导入浏览器获得的 Europe PMC PDF URL |
| `scripts/import_known_repository_pdfs.py` | 导入逐篇确认的官方/机构仓储 PDF |
| `scripts/import_ncbi_bookshelf_pdfs.py` | 导入 GeneReviews / NCBI Bookshelf PDF |
| `scripts/recover_molecular_vision_wayback.py` | 从 Internet Archive 恢复 Molecular Vision 原始 PDF |
| `scripts/download_journal_pdfs_with_uci_vpn.py` | 用正常网络/VPN 访问出版社 DOI；当前对剩余项效果有限 |

## 9. 新会话接手后的验证要求

每下载一篇都必须确认：

- 文件以 `%PDF` 开头
- 文件不是登录页、验证码页或错误页
- 至少 1 页
- 页数与论文类型合理
- 文本型 PDF 能提取正文；扫描型 PDF 需要保留完整页面
- PMID、DOI、题目与 manifest 行一致

导入成功后更新：

- `selected_source`
- `selected_pdf`
- `selected_status = selected`
- `priority_note`
- `needs_journal_or_uci_access.csv`

最终再次对所有 `selected_status == selected` 的 PDF 运行 `pypdf` 和 bundled Poppler `pdfinfo`。不要仅检查文件是否存在。

实时计数命令：

```bash
python3 - <<'PY'
import csv
p = 'output/retigene_papers/journal_priority/retigene_priority_manifest.csv'
rows = list(csv.DictReader(open(p)))
selected = [r for r in rows if r.get('selected_status') == 'selected']
remaining = [r for r in rows if r.get('selected_status') != 'selected']
print('total=', len(rows), 'selected=', len(selected), 'remaining=', len(remaining))
PY
```

## 10. 工作区注意事项

- 当前 git worktree 很脏，包含用户原有改动和大量未跟踪输出。
- 不要 `git reset --hard`、不要 checkout 覆盖、不要回滚不相关文件。
- 本次没有创建 commit。
- 损坏旧 PDF 被保留用于诊断，不要把它们重新算成成功。
- 用户要求可以继续使用 window/browser control，但认证、CAPTCHA 和 OTP 必须由用户本人完成。

## 11. 新窗口可直接发送的提示

```text
请先阅读仓库根目录的 RETIGENE_PAPER_RECOVERY_HANDOFF.md，然后从当前 in-app browser 的 ScienceDirect 页面继续。验证码已经通过，但还需要点击 Access through your organization，让我手动完成 UCI 登录。登录后先验证当前论文能否下载 PDF，再按 needs_journal_or_uci_access.csv 批量恢复。严格按 manifest 的 selected_status 计数，并对每个 PDF 做页数和可读性验证；不要回滚工作区的其他改动，也不要绕过验证码或付费墙。
```
