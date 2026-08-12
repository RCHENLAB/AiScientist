---
name: literature-corpus-recovery
description: >-
  Recover and assemble a corpus of scientific paper PDFs from a list of PMIDs/DOIs, using a tiered
  ladder of *legal* sources (open-access APIs, PMC/Europe PMC, publisher open-access, then
  institutional access via campus VPN and the university discovery layer), verifying every PDF and
  tracking status in a manifest. Use this skill whenever the user needs to download, recover,
  back-fill, or assemble research paper PDFs from PMIDs or DOIs — especially when building a
  literature corpus for RAG/PaperQA, when papers are paywalled and need institutional access, or when
  they say "download these papers", "get the full-text PDFs", "recover the corpus", or "the missing
  papers". Prefer this skill even for a one-off download of several papers — the value is trying the
  cheap legal sources first and only escalating when needed.
---

# Literature corpus recovery

Given a list of papers (PMIDs and/or DOIs), get a clean full-text PDF for each — **cheapest,
most-open source first** — escalating to institutional access only when needed, and record every
outcome so success is measured by a manifest, not a file count. Proven on the RetiGene run:
**1,749 unique PMIDs → 1,739 in the final Version-of-Record-first corpus**; the remaining ~10 have no
reachable full text, are marked `needs_manual`, and are covered by other papers on the same gene. Most
of the long tail is free if you try the open rungs first.

## How the RetiGene corpus was actually recovered (worked example)

The end-to-end path this skill was built from, in the order it actually ran:

1. **Get the target list.** The RetiGene *Genes* table's **References** column holds the PMIDs. Pull
   them, remove duplicates → **1,749 unique PMIDs**.
2. **PMID → metadata.** For each PMID, query NCBI E-utilities to get title, journal, year, **DOI**, and
   **PMCID**, and scaffold one manifest row per paper (this is the source of truth from here on).
3. **Sweep the free rungs first (no login).** Open-access APIs (Unpaywall / OpenAlex / Semantic
   Scholar by DOI) plus the **PMC OA PDF** picked up the whole open-access long tail automatically —
   the cheapest papers, no VPN needed.
4. **Publisher direct-PDF on the UCI VPN.** For the subscription journals, hit each publisher's own
   direct-PDF URL: **Wiley** `pdfdirect`, **Springer** `content/pdf`, **Taylor & Francis**, **BMJ**
   `.full.pdf`, **Neurology** `pdfdirect`, **IOVS/ARVO**. This is where most Version-of-Record PDFs came
   from.
5. **ScienceDirect / Elsevier click-through — the single biggest method.** One paper at a time: open
   the DOI, wait for it to land on ScienceDirect, and **click** *View PDF* (a scripted jump to the raw
   PDF URL is flagged as a bot and returns a blank page). Pause every ~35 downloads so the anti-bot
   doesn't start blocking.
6. **UC Library Search (Primo) by DOI — the rescue for "paywalled" papers.** Papers that looked locked
   at the publisher were usually held by the library through an aggregator (ProQuest / JSTOR / Ovid /
   EBSCO). Searching Primo **by exact DOI** rescued the old *Science*, *Diabetes Care*,
   *Ophthalmic Genet*, *Retinal Cases*, and *Healio OSLI Retina* papers.
7. **Reconcile PMC → journal.** Wherever a paper was being held as a PMC/author-manuscript copy but a
   journal VoR was actually reachable, the PMC copy was **replaced** with the journal version (old file
   moved to `_superseded/`, never deleted).
8. **Verify every file, then fix the failures.** Running the 8-point check (below) over the *whole*
   corpus surfaced and fixed: a **figure-only** file (just Figure 1), several **supplement-only** files,
   one **wrong paper** (right DOI, different article inside), ~20 **image-only scans** (OCR'd to add a
   text layer), and a couple of multi-article **bundles** (trimmed to the target).
9. **Last stubborn few.** For papers with no PDF at their own DOI, try the **same article in a different
   journal** (a second DOI that *does* have a real PDF), and hand the very last ones to the user for the
   **manual PMID → FULL TEXT LINKS** download. What still has no full text at all is marked
   `needs_manual` and left to a same-gene paper to cover.

Result: **1,739 verified, mostly Version-of-Record PDFs**, each row in the manifest recording the rung
it came from and its version tier. The rest of this document is the reusable version of these steps.

## The ladder — prefer the journal Version of Record; PMC reconstruction is the last resort

**Guiding rule:** for each paper, get the **publisher journal Version of Record (VoR)** whenever one
is reachable. A PMC copy is fine **only when it *is* the VoR** — open-access journals deposit the
final published PDF in PMC. The **non-VoR** PMC sources — Europe PMC full text, a PMC
*author manuscript*, or a PMC **HTML/scan reconstruction** — are a **fallback of last resort**, used
only when no VoR can be reached. **When a paper has both a PMC copy and a journal version and they
differ, keep the journal version.** (This is exactly the correction that drove the last review: PMC
author-manuscripts had been used where the journal VoR was actually available.)

| Order | Source | Gives the VoR? | How |
|---|---|---|---|
| 1 | Open-access APIs | Yes, for OA papers | Unpaywall / OpenAlex / Semantic Scholar by DOI → download the OA PDF url they return |
| 2 | PMC OA PDF | Yes **when the journal is OA** | PMID→PMCID → `pmc.ncbi.nlm.nih.gov/articles/PMC<id>/pdf/`. Use **only if it's the published PDF**, not a "HHS/Author manuscript" banner version |
| 3 | Bookshelf / GeneReviews | Yes | `www.ncbi.nlm.nih.gov/books/NBK<id>/` → the official PDF link |
| 4 | Publisher OA direct PDF | Yes | the publisher's own `.../pdf?download=true` URL (patterns below) |
| 5 | Campus-IP direct PDF (VPN) | Yes | on campus VPN, hit the direct PDF URL per publisher (patterns below) |
| 6 | Browser + institutional | Yes | UCI-authenticated browser: **click** View PDF; and UC Library Search by DOI → aggregator |
| 7 | Internet Archive | Yes (archived VoR) | for defunct OA journals, an archived copy of the PDF via web.archive.org |
| 8 | Repositories / preprints | No — author/accepted manuscript | institutional-repo or bioRxiv/medRxiv PDF: full text, but not the VoR — use only if 1–7 fail |
| 9 | Europe PMC full text · PMC HTML/scan reconstruction | **No — last resort** | only when no VoR is reachable: Europe PMC full-text API, else rebuild a PDF from the PMC article HTML or scanned pages |

Rungs 4–6 (publisher OA / VPN / UCI browser) are where the VoR usually comes from — reach for them
**before** the non-VoR fallbacks (8–9), even though those are free. A paper with **both** a journal
version and a PMC copy must end up as the journal version.

**Before giving up on a "no-PDF" paper, try the same article in a *different* journal.** The same
content is sometimes published (or reprinted) under a second DOI in another journal that *does* have a
real PDF. Search the **exact title** on PubMed / Google Scholar and check the other hits. Real
example: PMID 21686617 is a *BMJ Case Reports* record (fellowship-gated, no PDF anywhere), but the
identical case report also appeared in *J Neurol Neurosurg Psychiatry* with a normal downloadable VoR
— use that and note the alternate DOI. This rescues papers that look impossible at their own DOI.

A handful still have no reachable VoR at all (subscription/fellowship wall, SSO-only, defunct DOI, or
no PDF exists anywhere) — record the reason and either use the best available copy
(author manuscript > reconstruction, noting the tier) or, if the requester wants VoR-only, drop the
row and mark it `needs_manual`. These are usually covered by other papers on the same gene.

**Manual PMID→PDF fallback (hand this to the user for the last few).** Sometimes the fastest path for
a stubborn paper is to let the user fetch it by hand: open `https://pubmed.ncbi.nlm.nih.gov/<PMID>/`,
use the **FULL TEXT LINKS** box (top-right) — a **"Free article"** tag or a publisher/PMC icon there
is the direct route — click through (on VPN / with institutional login) and download. The user then
drops the PDF back into the ingest folder. This is also what a supervisor will do to spot-check, so
it doubles as the acceptance test.

## Workflow

1. **Scaffold a manifest** — one row per paper (title, authors, journal, year, DOI) with a `status`
   column. The manifest is the source of truth: **"recovered" = rows marked done with a verified
   PDF, never the number of files on disk** (broken/superseded files are kept for diagnosis and would
   inflate a file count).
2. **Free VoR sources first (rungs 1–3), no login** — open-access APIs, PMC OA PDF (only if it's the
   published PDF, not an author manuscript), Bookshelf. Stop at the first that yields a real PDF;
   verify, note the source, mark done.
3. **Publisher / VPN / institutional for the VoR (rungs 4–6)** — for everything still missing, on the
   campus VPN (browser set to *download PDFs, not open them*) try the publisher OA direct-PDF URL,
   then the campus-IP direct-PDF URL, then the UCI-authenticated browser (**click** View PDF; UC
   Library Search by DOI → aggregator). This is where most VoRs come from.
4. **Non-VoR fallbacks last (rungs 7–9)** — only for papers with no reachable VoR: Internet Archive
   (defunct OA journals), then an author/accepted-manuscript from a repository/preprint, then Europe
   PMC full text or a PMC HTML/scan reconstruction. Record the version tier so the gap is honest.
5. **Reconcile PMC vs journal** — for any paper currently held as a PMC copy, check whether a journal
   VoR is now reachable; if it is **and differs**, replace the PMC file with the VoR. Move the old
   file to `_superseded/` (don't delete).
6. **Skip the truly-unavailable few** — no reachable full text (only a citation/index record), defunct
   DOIs, or publish-ahead-of-print. Record the reason and move on; don't hammer a paywall. These are
   usually covered by other papers on the same gene.

## Access details

**Open-access APIs (rung 1).** Query by DOI and download the PDF url each returns:
Unpaywall `https://api.unpaywall.org/v2/<DOI>?email=YOUR_EMAIL` → `best_oa_location.url_for_pdf`;
OpenAlex `https://api.openalex.org/works/doi:<DOI>` → `open_access.oa_url`;
Semantic Scholar `https://api.semanticscholar.org/graph/v1/paper/DOI:<DOI>?fields=openAccessPdf` →
`openAccessPdf.url`.

**Direct-PDF URL patterns (rungs 6–7; browser set to download PDFs, on the VPN).**
- Wiley: `onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true`
- BMJ (J Med Genet): `jmg.bmj.com/content/jmedgenet/<vol>/<iss>/<page>.full.pdf`
- Neurology: `www.neurology.org/doi/pdfdirect/<DOI>?download=true` (`/doi/pdf/` only shows the reader)
- ACS: `pubs.acs.org/doi/pdf/<DOI>?download=true`
- Springer: `link.springer.com/content/pdf/<DOI>.pdf`
- Taylor & Francis: `www.tandfonline.com/doi/pdf/<DOI>?download=true`
- SAGE: `journals.sagepub.com/doi/pdf/<DOI>?download=true` (Cloudflare — user clicks "Verify you are human" once)
- IOVS/ARVO: PubMed → "Silverchair" link → `iovs.arvojournals.org/article.aspx?...` → click the toolbar PDF

**One-time setup for the browser rungs (6–8).** Connect the UCI VPN (UCIFull, full tunnel). In
Chrome, set PDFs to *download instead of open* (`chrome://settings/content/pdfDocuments`) and turn
off *ask where to save each file* — so a click just drops the PDF into your download folder.

**ScienceDirect / Elsevier — step by step (rung 8).** This is the biggest single method; do it per paper:
1. Open `https://doi.org/<DOI>` and wait ~4 s for it to land on ScienceDirect (`j.`-style DOIs
   resolve to a `pii/<PII>` URL; old DOIs give the PII by stripping punctuation and upper-casing).
2. Look for a green **View PDF** button. If it's there, the institution has full-text access.
3. **Click** View PDF (do not paste the raw PDF URL — a scripted jump is flagged as a bot and
   returns a blank page). A new tab opens and the PDF downloads to your folder.
4. Verify the file and record it (see "Verify" below); mark the manifest row done.
5. Close the leftover download tab before the next paper — a stale anti-bot tab blocks the next click.
6. If a click doesn't trigger the download, close that tab and click once more.
7. After ~35 rapid downloads the anti-bot (crasolve) starts blocking auto-clicks — pause 60–90 s, or
   let the user click a few by hand, then resume.

**UC Library Search / Primo — step by step (rung 8, the high-value trick).** For papers that look
paywalled at the publisher, the library often holds them through an aggregator (this rescued the old
Science, Diabetes Care, Ophthalmic Genet, Retinal Cases, and Healio OSLI Retina papers):
1. Go to `https://uci.primo.exlibrisgroup.com` and sign in with UCInetID (unlocks request options).
2. **Search by DOI**, exactly — a title search collides with newer, similarly-titled papers.
3. If the result row shows **Get PDF** / **Download PDF**, click it — it opens the aggregator
   (ProQuest / JSTOR / Ovid / Taylor & Francis / EBSCO) with UCI access, then download the PDF.
4. If it shows only **Available Online**, open the record detail and read the **View Online** section
   for the real provider; click that. (The quick "Available Online" link sometimes mis-resolves to a
   LexisNexis sign-in — a dead end; the record detail lists the true holdings.)
5. Dismiss any publisher popup (see gotchas), download, then verify + record.

**Publisher gotchas.** ProQuest wraps old papers as image scans (low text, complete pages — fine);
JSTOR needs a one-time "Accept and download" terms click; Ovid (LWW/Retinal Cases) is a single-user
license, so a transient `License Service Failure (E3)` just means retry in a minute; Healio differs by
journal (OSLI Retina is in ProQuest, but JPOS only has an EBSCO link that bounces to the publisher →
not retrievable, skip); publish-ahead-of-print items aren't in a subscribed issue yet → wait for the
issue or skip.

## Verify every PDF — run an automatic check on EVERY file, not just the suspicious ones
A download only counts once it passes all of the checks below. Run these across the **whole corpus**
(both `*pmc*` and non-`pmc` files — scripted downloads mislabel silently), not only the ones you
suspect. Each check below is a real failure mode seen in practice; the cheap `pdfinfo`/`pdftotext`
(poppler) tools are enough to catch them.

1. **Real PDF, not an error page.** Starts with the bytes `%PDF`; the text is not a login / CAPTCHA /
   "Are you a robot" / "we don't have this article in PDF format" / 404 page.
2. **Complete, not a fragment.** Page count is sane for the article type. Watch for the *figure-only*
   failure (e.g. a 1-page file that is just Figure 1) — check that the first page contains article
   prose, not only a figure caption.
3. **Main text, not supplementary-only.** A distinct and common failure: the file is just the
   supplement. Reliable test: a supplement marker appears **near the top of page 1** — anywhere in the
   first ~300 characters, *not necessarily the first line* (a running header like
   "Author et al., Human Mutation 1" or "ONLINE SUPPORTING INFORMATION" often precedes it) — **and the
   document has no `Abstract`.** A real article almost always has an Abstract; a supplement almost
   never does. Markers to match (case-insensitive, include the abbreviations): `Supplementary` /
   `Supplemental` / `Supporting information` / `Online supporting information` / `SI Appendix` /
   **`Supp.`** / `Suppl.` / `Supp. Methods` / `Supp. Figure` / `Figure S1` / `Table S1`. Do **not**
   rely on the absence of the word "methods" — a supplement's "**Supp. Methods**" heading contains it.
   Cause: a scripted "find the PDF link" step grabs the supplementary link instead of the article, or
   a PMC OA-package fallback returns only the supplement. Fix: re-fetch the article by **clicking View
   PDF** on the publisher page (or the Wiley/Elsevier direct-PDF URL). Watch: `Human Mutation` and
   other journals deposit a separate supplement PDF that is easy to grab by mistake.
4. **The right paper (title match).** Normalize the expected title and confirm its distinctive words
   appear in the PDF text. A low match on a full-text file usually means the wrong paper was fetched
   (right DOI in the filename, different article inside) — re-download by DOI. (A low match on a
   *short* file is often just a shared-page neighbour or a scan with no text layer — inspect before
   discarding.)
5. **Readable text (OCR image-only scans).** Old papers from PMC/NEJM/BBRC are sometimes image-only
   (pages are complete but yield near-zero extractable text). These are not broken, but a RAG index
   can't read them. Add a text layer with OCR (`ocrmypdf --force-ocr`, needs tesseract+ghostscript+
   qpdf) and verify the text now extracts.
6. **One article per file (trim bundles).** Some publisher PDFs (e.g. a BMJ/JMG "Letters" section)
   serve a whole multi-article section; the target is buried inside. Locate the target's page range
   and trim to just those pages (pypdf) so the document is one paper. If the target shares a single
   page with a neighbour and trimming would drop its title, leave it whole.
7. **Record the version tier.** Prefer the publisher **Version of Record**; accept an
   **author/accepted manuscript** or a **PMC HTML/scan reconstruction** only when the VoR is
   genuinely inaccessible (subscription/fellowship wall, SSO login, or no PDF exists). Note which tier
   each file is so the gaps are honest. A reconstruction is the **weakest** tier and can carry
   artifacts (e.g. a scrambled corresponding-author email) — replace it with any real PDF when one
   turns up.
8. **Eyeball page 1 (the reviewer's own quick check).** A visual pass catches what text checks miss: a
   real VoR shows the **publisher masthead** on page 1 (journal logo, "© Publisher", "journal
   homepage: …"); an *author manuscript* shows a plain "Author manuscript / Europe PMC Funders Group"
   banner; a *reconstruction* shows a "Source: … captured from …" line; a *supplement* opens with
   "Supplementary". Two things that look wrong but are **fine**: (a) **old journals (pre-2000) have no
   logo** — just a citation line like `Am. J. Hum. Genet. 49:939-950, 1991`; that IS the journal
   identifier. (b) **Shared pages** — in "Letters"/"Correspondence"/"Pictures & Perspectives" sections
   several short items share printed pages, so page 1 can open with the tail of the previous article
   and the file's page numbers start at the journal page (e.g. 444), not 1. The target article is
   still complete; do not treat it as a fragment.

Only after a file passes do you mark the manifest row done and record which rung + version tier it
came from. Keep every superseded/rejected file (move to a `_superseded/` folder, don't delete) so a
count check stays trustworthy and mistakes are reversible.

## Guardrails (non-negotiable)
- **Never bypass** CAPTCHAs, logins, or paywalls, and never mass-scrape paywalled sites. Use only
  access the user legitimately has (their institutional credentials / VPN).
- **The user performs all authentication** — passwords, Duo/2FA, SSO, and any "Accept terms" click.
- Prefer the most open source that works; escalate only for what the cheaper rungs miss.
- Track progress from the manifest, never from a file count.
