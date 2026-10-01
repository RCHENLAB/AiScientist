"""Do the models KNOW what the reports got wrong? Ask each error as a short standalone question."""
import json, os, sys, time, urllib.request, concurrent.futures as cf
KEY = sys.argv[1]
CTX = ("Single-nucleus RNA-seq of mouse retina, DDX41 mutant vs WT, ONE library per condition (no biological "
       "replicates). Median UMIs per nucleus: mutant 3,078, WT 1,916 (1.6x).")
P = {
 "depth": CTX + " After per-cell total-count normalization + log1p and a per-cell-type Wilcoxon test, ribosomal/translation "
   "genes (Rps20, Ubb, Hmgn2, Eef1a1) are higher in the mutant in ALL five tested cell types (rods, cones, bipolar, amacrine, "
   "Muller glia) with similar fold changes. Should 'DDX41 loss triggers a pan-retinal translational stress response' be the "
   "headline finding? Answer YES or NO first, then explain in <=100 words.",
 "ambient": CTX + " In the mutant's AMACRINE cells the most down-regulated genes are Rho, Gnat1 and Pde6g (log2FC -1.7 to -1.9). "
   "Give the single most likely explanation first, then explain in <=80 words.",
 "lognorm": "Does per-cell total-count normalization followed by log1p remove the effect of a 1.6x difference in sequencing "
   "depth between two libraries on a per-cell Wilcoxon differential-expression test? Answer YES or NO first, then <=80 words.",
 "tables": CTX + " Two tables from the same analysis. Table 1 (dataset profile, cells per arm): Amacrine DDX41 392 / WT 258; "
   "Cone DDX41 112 / WT 499. Table 2 (DE step summary, cells tested): Amacrine WT 469 / DDX41 181; Cone WT 310 / DDX41 301. "
   "Total cells: DDX41 6,260, WT 9,047. In ONE sentence, state how amacrine and cone abundance differ between arms.",
}
ARMS = {
 "qwen3.8-27b(low)": {"model": "qwen/qwen3.8-27b", "reasoning": {"effort": "low"}},
 "qwen3.6-35b-a3b":  {"model": "qwen/qwen3.6-35b-a3b"},
 "claude-sonnet-5":  {"model": "anthropic/claude-sonnet-5"},
}
def ask(arm, q, rep):
    spec = ARMS[arm]; body = {"model": spec["model"], "messages": [{"role": "user", "content": P[q]}], "max_tokens": 6000}
    if "reasoning" in spec: body["reasoning"] = spec["reasoning"]
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    for _ in range(3):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=300))
            return arm, q, rep, (r["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:
            err = str(e); time.sleep(3)
    return arm, q, rep, "ERROR " + err
jobs = [(a, q, r) for a in ARMS for q in P for r in range(2)]
with cf.ThreadPoolExecutor(12) as ex:
    res = list(ex.map(lambda t: ask(*t), jobs))
json.dump(res, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
for arm, q, rep, a in sorted(res):
    print(f"--- {q:8s} {arm:17s} #{rep}: {a[:420]}".replace("\n", " "))
