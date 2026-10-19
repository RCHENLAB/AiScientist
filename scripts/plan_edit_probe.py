"""Pick the plan edits to ask for, and grade what came back — without needing the biology.

The reviewer testing plan-mode revision has to answer two questions that are not about the product
at all: *which number in this plan is safe to ask to change*, and *did anything else move?* The
first needs single-cell method knowledge; the second needs a careful character-by-character read of
six paragraphs of prose. Both are why plan-revision regressions historically went unnoticed — the
one that mattered most (``experiments/plan_revision_ab``) was found only because someone diffed two
plans by hand.

This script does both mechanically:

    targets  — read a plan card, list every numeric setting it states, and for the ones we can
               name, print a scientifically legitimate change to ask for, the sentence to type,
               and the grading command to run afterwards. It also flags, in advance, which steps
               would be left contradicting the edit (the product is supposed to warn about those)
               and which settings appear in more than one step (the ambiguity case).

    grade    — diff the plan you got back against the one you started with and print a verdict.
               "Only the step I named changed" is checked by EXACT string equality on every other
               step, which is stricter than what the console shows the reviewer: ``diff_plans``
               treats a reworded step as the same step, so a silent rewording is invisible there
               and visible here. Both views are printed, and a disagreement between them is
               itself a finding.

Two plan formats, detected per file:

  * **a plan document** (`report/plan.yaml`, or the YAML shown on the card) — the default since
    `AISCIENTIST_PLAN_YAML` is on. Each step carries `tool` + `settings`, so the settings are read
    STRUCTURALLY and the change list comes from `plan_doc.apply_edit` — the same function the
    product uses to adopt a hand edit. No regex guessing.
  * **prose steps**, one per line (what the card showed before the document, and what it still
    shows with the flag off). Settings are then recovered with production's `_settings_in` regex.

Usage (needs the repo venv for PyYAML, and PYTHONPATH so a worktree imports ITS OWN src):
    PYTHONPATH=$PWD/src .venv/bin/python scripts/plan_edit_probe.py targets before.yaml
    PYTHONPATH=$PWD/src .venv/bin/python scripts/plan_edit_probe.py grade before.yaml after.yaml \
        --step 3 --param resolution --to 1.0
    ... grade before.yaml after.yaml --mode unchanged   # noise / question tests
    ... grade before.yaml after.yaml --mode structural # you asked to add/remove/move a step

See docs/plan_edit_test_protocol.md for the test matrix.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# The SAME helpers production uses to decide what counts as a setting and what counts as a changed
# step. Deliberate: a grader with its own private idea of "a setting" would score the product
# against a spec the product has never seen. ``_settings_in`` is private and imported anyway —
# this script is part of the repo it grades. (`bioagent` is only a compat alias; import the real
# package so a rename of the alias cannot silently change what is being graded.)
from aiscientist.agents.plan_doc import (  # noqa: E402
    PlanError,
    apply_edit,
    parse_plan_yaml,
)
from aiscientist.agents.research_lab import (  # noqa: E402
    _settings_in,
    diff_plans,
    stale_downstream_settings,
)

# --- the parameter cheat-sheet ------------------------------------------------------------------
# What a non-biologist needs in order to ask for a change that a reviewer would consider
# reasonable: what the knob does in one line, and a DIFFERENT value that is equally defensible.
# Every alternative here is a value people actually publish with — the test is about whether the
# system applies the edit faithfully, so the edit must not be one a good planner should refuse.
#
#   meaning  — one line, plain language
#   effect   — which way the results move, so the run afterwards can be sanity-checked
#   pairs    — (from, to) — if the plan states `from`, ask for `to`; else ask for `default_to`
_PARAMS: "dict[str, dict[str, str]]" = {
    "resolution": {
        "meaning": "聚类粒度 — Leiden/Louvain 把细胞分成多少群",
        "effect": "调大 = 群更多更细；调小 = 群更少更粗",
        "pairs": "0.5>1.0 1.0>0.5 0.8>1.2 0.4>0.8",
        "default_to": "1.0",
    },
    "n_neighbors": {
        "meaning": "近邻图里每个细胞连多少个邻居",
        "effect": "调大 = 结构更平滑、更看全局；调小 = 更看局部细节",
        "pairs": "15>30 10>20 30>15",
        "default_to": "30",
    },
    "n_pcs": {
        "meaning": "降维后拿多少个主成分往下用",
        "effect": "调大 = 保留更多变异（也更多噪声）；调小 = 更干净但可能丢信号",
        "pairs": "50>30 30>50 20>40",
        "default_to": "30",
    },
    "n_comps": {
        "meaning": "主成分个数（同 n_pcs 的另一种写法）",
        "effect": "调大 = 保留更多变异；调小 = 更干净",
        "pairs": "50>30 30>50",
        "default_to": "30",
    },
    "min_genes": {
        "meaning": "质控 — 检出基因数少于这个数的细胞被丢掉",
        "effect": "调大 = 更严，剩下的细胞更少但质量更高",
        "pairs": "200>500 500>200 100>300",
        "default_to": "500",
    },
    "min_cells": {
        "meaning": "质控 — 在少于这么多细胞里出现过的基因被丢掉",
        "effect": "调大 = 更严，基因数变少",
        "pairs": "3>10 10>3 5>20",
        "default_to": "10",
    },
    "pct_counts_mt": {
        "meaning": "线粒体基因占比上限（%）— 超过就当作濒死细胞丢掉",
        "effect": "调大 = 更宽松，保留更多细胞",
        "pairs": "5>10 10>5 20>10",
        "default_to": "10",
    },
    "n_top_genes": {
        "meaning": "选多少个高变基因进入下游分析",
        "effect": "调大 = 输入更多基因，信号和噪声一起变多",
        "pairs": "2000>3000 3000>2000 1000>2000",
        "default_to": "3000",
    },
    "padj": {
        "meaning": "多重检验校正后的显著性阈值（FDR）",
        "effect": "调小 = 更严，显著基因更少",
        "pairs": "0.05>0.01 0.01>0.05 0.1>0.05",
        "default_to": "0.01",
    },
    "fdr": {
        "meaning": "假发现率阈值（同 padj）",
        "effect": "调小 = 更严",
        "pairs": "0.05>0.01 0.01>0.05",
        "default_to": "0.01",
    },
    "alpha": {
        "meaning": "显著性水平阈值",
        "effect": "调小 = 更严",
        "pairs": "0.05>0.01 0.01>0.05",
        "default_to": "0.01",
    },
    "pval": {
        "meaning": "未校正 p 值阈值",
        "effect": "调小 = 更严",
        "pairs": "0.05>0.01 0.01>0.05",
        "default_to": "0.01",
    },
    "log2fc": {
        "meaning": "差异倍数下限（log2 尺度）— 变化多大才算数",
        "effect": "调大 = 只留变化幅度更大的基因",
        "pairs": "0.25>1.0 1.0>0.25 0.5>1.0",
        "default_to": "1.0",
    },
    "logfc": {
        "meaning": "差异倍数下限（同 log2fc）",
        "effect": "调大 = 只留变化更大的基因",
        "pairs": "0.25>1.0 1.0>0.25",
        "default_to": "1.0",
    },
    "top_n": {
        "meaning": "每组报告多少个基因",
        "effect": "纯展示口径，不改统计",
        "pairs": "50>100 100>50 20>50",
        "default_to": "100",
    },
    "n_genes": {
        "meaning": "每组取多少个基因往下走",
        "effect": "调大 = 下游拿到更多基因",
        "pairs": "50>100 100>50",
        "default_to": "100",
    },
    "min_cells_per_group": {
        "meaning": "一个组至少要有多少细胞才参与这个统计",
        "effect": "调大 = 更严，小群体被排除",
        "pairs": "10>20 20>10 3>10",
        "default_to": "20",
    },
    "min_pct": {
        "meaning": "至少多大比例的细胞表达这个基因，它才被检验",
        "effect": "调大 = 更严，稀有表达基因被排除",
        "pairs": "0.1>0.25 0.25>0.1",
        "default_to": "0.25",
    },
    "random_state": {
        # The one edit that carries NO methodological opinion at all — useful as a pure mechanism
        # probe when you want to test the plumbing without asserting anything scientific.
        "meaning": "随机种子 — 只影响可复现性，不影响方法对错",
        "effect": "结论不该变；纯机制测试用这个最安全",
        "pairs": "0>42 42>0 1>42",
        "default_to": "42",
    },
    "seed": {
        "meaning": "随机种子（同 random_state）",
        "effect": "结论不该变；纯机制测试用这个最安全",
        "pairs": "0>42 42>0",
        "default_to": "42",
    },
}

# Settings whose value the step BELOW is likely to restate — the pair that produces a plan
# contradicting itself when only one of the two is edited. Used to rank which target to try first.
_CHAINED = {"padj", "fdr", "alpha", "pval", "log2fc", "logfc"}


class Plan:
    """One plan, from either format, with the per-step settings read the best way available.

    ``doc`` is the parsed :class:`PlanDoc` for a plan document and ``None`` for prose steps —
    the grader uses it to get the product's OWN change list instead of a string diff.
    """

    def __init__(self, path: "str | Path") -> None:
        self.path = str(path)
        text = Path(path).read_text(encoding="utf-8")
        self.doc = None
        # A plan document announces itself: `plan: <n>` + `steps:` at the top level. Anything else
        # is prose. Parsed with the product's parser, so a file the product would REFUSE is refused
        # here too, with its own message, rather than being silently graded as prose.
        if re.search(r"^plan\s*:\s*\d+\s*$", text, re.M) and re.search(r"^steps\s*:", text, re.M):
            self.doc = parse_plan_yaml(text)
            self.steps = self.doc.agenda()
            self.settings = [dict(s.settings) for s in self.doc.steps]
            self.tools = [s.tool or "" for s in self.doc.steps]
            self.ids = [s.id for s in self.doc.steps]
            return
        self.steps = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            # "1." / "1)" / "Step 1:" / "- " — how a pasted card carries its numbering
            s = re.sub(r"^(?:step\s*)?\d+\s*[.)：:]\s*|^[-*•]\s*", "", s, flags=re.I)
            if s:
                self.steps.append(s)
        # No structured settings in prose: recover them with production's own regex.
        self.settings = [_settings_in(s) for s in self.steps]
        self.tools = [""] * len(self.steps)
        self.ids = [f"{i + 1}" for i in range(len(self.steps))]

    @property
    def kind(self) -> str:
        return "plan document (structured settings)" if self.doc is not None else "prose steps"

    def setting(self, idx: int, param: str) -> "str | None":
        """``param``'s value at 0-based step ``idx``, as a string, or ``None``."""
        if not 0 <= idx < len(self.settings):
            return None
        for key, value in self.settings[idx].items():
            if str(key).lower() == param.lower():
                return _fmt_value(value)
        return None


def _fmt_value(value: Any) -> str:
    """A setting value as the plan states it — ``1.0`` not ``1``, so it compares to ``--to 1.0``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") and isinstance(value, int) else text


def load_plan(path: "str | Path") -> "list[str]":
    """Backwards-compatible helper: just the step strings."""
    return Plan(path).steps


def _suggest(param: str, current: str) -> "str | None":
    """A defensible different value for ``param``, given what the plan states now."""
    entry = _PARAMS.get(param)
    if not entry:
        return None
    for pair in entry["pairs"].split():
        src, dst = pair.split(">")
        if src == current:
            return dst
    return entry["default_to"] if entry["default_to"] != current else None


def _stale_downstream(plan: "Plan", idx: int, param: str, old: str) -> "list[int]":
    """1-based steps AFTER ``idx`` that still state ``param`` at ``old``.

    Same question as production's ``stale_downstream_settings``, asked against the STRUCTURED
    settings when the plan is a document (so it does not depend on the prose regex), and against
    the regex when it is prose."""
    return [j + 1 for j in range(idx + 1, len(plan.steps)) if plan.setting(j, param) == old]


def cmd_targets(args: argparse.Namespace) -> int:
    plan = Plan(args.plan)
    if not plan.steps:
        print(f"no steps parsed from {args.plan}", file=sys.stderr)
        return 2
    print(f"# {len(plan.steps)} steps read from {args.plan} — {plan.kind}\n")

    # Which settings appear in more than one step: naming one of those without a step number is
    # the ambiguity case the product is supposed to interrupt on (test A7).
    seen: dict[str, list[int]] = {}
    for i, settings in enumerate(plan.settings, 1):
        for k in settings:
            seen.setdefault(str(k).lower(), []).append(i)

    ranked: list[tuple[int, str]] = []           # (priority, printable block)
    for i, step in enumerate(plan.steps, 1):
        settings = plan.settings[i - 1]
        head = step if len(step) <= 110 else step[:107] + "..."
        tool = plan.tools[i - 1]
        print(f"── step {i} ──  {head}" + (f"\n     tool: {tool}" if tool else ""))
        if not settings:
            print("     (no setting stated — not a target for a number edit)\n")
            continue
        for raw_param, raw_value in settings.items():
            param, current = str(raw_param).lower(), _fmt_value(raw_value)
            entry = _PARAMS.get(param)
            if entry is None:
                print(f"     • {param}={current}  — 未收录的参数，先别用它测"
                      f"（我们不确定改成什么算合理）")
                continue
            new = _suggest(param, current)
            if new is None:
                continue
            stale = _stale_downstream(plan, i - 1, param, current)
            block = [
                f"     • {param}: {current} → {new}    {entry['meaning']}",
                f"       方向：{entry['effect']}",
                f"       改法 1 · 手改 YAML（S0，没有模型参与，应该 100% 准）："
                f"把第 {i} 步 settings 里的 {param} 改成 {new}，点 Save",
                f"       改法 2 · 用话说（S1，中）：{_prompt_zh(i, param, current, new)}",
                f"       改法 2 · 用话说（S1，EN）：{_prompt_en(i, param, current, new)}",
                f"       跑完后打分：PYTHONPATH=$PWD/src .venv/bin/python scripts/plan_edit_probe.py "
                f"grade {args.plan} after.yaml --step {i} --param {param} --to {new}",
            ]
            if stale:
                block.append(
                    f"       ⚠ 下游第 {', '.join(map(str, stale))} 步也写着 {param}={current} —— "
                    f"改完之后产品**应该**主动提醒这几步还是旧值。没提醒 = 一个 finding。")
            if len(seen.get(param, [])) > 1:
                block.append(
                    f"       ⚠ {param} 在第 {', '.join(map(str, seen[param]))} 步都出现 —— "
                    f"不带步号地说「把 {param} 改成 X」，产品**应该**反问你指哪一步。")
            print("\n".join(block) + "\n")
            ranked.append(((0 if stale else 1 if param in _CHAINED else 2), f"step {i} · {param}"))

    if ranked:
        ranked.sort()
        print("建议的测试顺序（先测能连锁出问题的）：")
        for _, label in ranked[:5]:
            print(f"  - {label}")
    return 0


def _prompt_zh(step: int, param: str, old: str, new: "str | None") -> str:
    # No protective clause ("其他步骤别动") on purpose — see the protocol doc: adding one turns the
    # test into "does coaching help", and a real user does not write that.
    return f"第 {step} 步的 {param} 用 {new}，不要 {old}。"


def _prompt_en(step: int, param: str, old: str, new: "str | None") -> str:
    return f"In step {step}, use {param} {new} instead of {old}."


def _unified(before: str, after: str) -> str:
    """A word-level before/after for one step, so the drift is readable without squinting."""
    return f"       − {before[:200]}\n       + {after[:200]}"


def _describe(change: "dict[str, Any]") -> str:
    k = change.get("kind")
    if k == "setting":
        return (f"step {change['step']} ({change['id']}) setting {change['param']}: "
                f"{change.get('before')} → {change.get('after')}")
    if k in ("added", "removed"):
        return f"step {change['step']} ({change['id']}) {k}: {str(change.get(k == 'added' and 'after' or 'before'))[:120]}"
    if k == "moved":
        return f"step {change['step']} ({change['id']}) moved from position {change.get('before')}"
    return (f"step {change['step']} ({change['id']}) {k}:\n"
            f"       − {str(change.get('before') or '')[:200]}\n"
            f"       + {str(change.get('after') or '')[:200]}")


def _grade_document(args: argparse.Namespace, before: "Plan", after: "Plan") -> bool:
    """Grade two plan DOCUMENTS with the product's own change list (``plan_doc.apply_edit``)."""
    _, changes, notes = apply_edit(before.doc, after.doc)
    count_delta = len(after.steps) - len(before.steps)
    target = args.step or 0
    mine = [c for c in changes if c.get("step") == target]
    other = [c for c in changes if c.get("step") != target]
    ok = True

    if args.mode == "unchanged":
        print(f"{'PASS' if not changes else 'FAIL'}  plan unchanged "
              f"({len(changes)} change(s), {count_delta:+d} step(s) in length)")
        ok = not changes
    else:
        if target:
            hit = bool(mine)
            print(f"{'PASS' if hit else 'FAIL'}  target_hit — step {target} actually changed")
            ok &= hit
            for c in mine:
                print(f"       {_describe(c)}")
            if args.param and args.to:
                now = after.setting(target - 1, args.param)
                intent = now == args.to
                print(f"{'PASS' if intent else 'FAIL'}  intent_ok — step {target} now has "
                      f"{args.param}={now or '(absent)'}, wanted {args.to}")
                ok &= intent
        if args.mode == "edit":
            clean = not other and count_delta == 0
            print(f"{'PASS' if clean else 'FAIL'}  no collateral — {len(other)} change(s) to "
                  f"steps you did not name, {count_delta:+d} step(s) in length")
            ok &= clean
        else:
            drift = [c for c in other if c.get("kind") in ("setting", "tool", "removed")]
            print(f"INFO  structural mode — {len(other)} change(s) elsewhere, "
                  f"{count_delta:+d} step(s) in length (rewording allowed)")
            print(f"{'PASS' if not drift else 'FAIL'}  no silent setting/tool/step loss in the steps "
                  f"you did not mention — {len(drift)}")
            ok &= not drift
            other = drift
        for c in other:
            print(f"  NOT ASKED FOR: {_describe(c)}")

    # The product's own parser flags a step whose PROSE still states a value its settings no longer
    # hold. That is the plan contradicting itself inside one step, and it is what the researcher
    # reads — so it counts even though the pinned number would be right.
    for n in notes:
        print(f"⚠ FINDING ({n.get('kind')}): step {n.get('step')} ({n.get('id')}) — "
              f"{str(n)[:220]}")
        ok = False
    return ok


def cmd_grade(args: argparse.Namespace) -> int:
    try:
        before, after = Plan(args.before), Plan(args.after)
    except PlanError as exc:
        print(f"the plan did not parse: {'; '.join(exc.problems)}", file=sys.stderr)
        return 2
    print(f"# before: {len(before.steps)} steps ({before.kind})   "
          f"after: {len(after.steps)} steps ({after.kind})\n")

    if before.doc is not None and after.doc is not None:
        ok = _grade_document(args, before, after)
    else:
        if (before.doc is None) != (after.doc is None):
            print("⚠ the two files are in DIFFERENT formats — grading as prose; re-capture both "
                  "the same way before trusting this.\n")
        ok = _grade_prose(args, before.steps, after.steps)

    # The contradiction the product is supposed to warn about, computed independently here so you
    # can tell "it did not warn" apart from "there was nothing to warn about".
    if args.step and args.param and args.to:
        old = before.setting(args.step - 1, args.param)
        if old and old != args.to:
            stale = _stale_downstream(after, args.step - 1, args.param, old)
            print(f"\nEXPECT-WARNING  downstream steps still at {args.param}={old}: "
                  f"{stale or 'none'}"
                  + ("   → the run's feed MUST carry a stale-threshold warning naming these"
                     if stale else ""))

    print(f"\n=== {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


def _grade_prose(args: argparse.Namespace, before: "list[str]", after: "list[str]") -> bool:
    """Grade two PROSE plans by exact string equality on the steps you did not name."""
    count_delta = len(after) - len(before)
    target = args.step - 1 if args.step else None
    collateral: list[tuple[int, str, str]] = []
    for i in range(min(len(before), len(after))):
        if i == target:
            continue
        if before[i].strip() != after[i].strip():
            collateral.append((i + 1, before[i], after[i]))

    # What the CONSOLE would have told the reviewer about the same pair of plans. It matches steps
    # by token overlap, so a step that was reworded but stayed recognisable is reported as
    # unchanged. Printed next to the strict view precisely so the gap is visible.
    prod = diff_plans(before, after)
    ok = True

    if args.mode == "unchanged":
        same = [b.strip() for b in before] == [a.strip() for a in after]
        print(f"{'PASS' if same else 'FAIL'}  plan unchanged  "
              f"({'identical' if same else f'{len(collateral)} step(s) differ, count_delta={count_delta}'})")
        ok = same
        for n, b, a in collateral:
            print(f"  step {n} moved:\n{_unified(b, a)}")
    else:
        if target is not None:
            hit = target < len(after) and target < len(before) and \
                before[target].strip() != after[target].strip()
            print(f"{'PASS' if hit else 'FAIL'}  target_hit — step {args.step} actually changed")
            ok &= hit
            if args.param and args.to and target < len(after):
                now = _settings_in(after[target]).get(args.param.lower())
                intent = now == args.to
                print(f"{'PASS' if intent else 'FAIL'}  intent_ok — step {args.step} now states "
                      f"{args.param}={now or '(absent)'}, wanted {args.to}")
                ok &= intent
                if hit:
                    print(_unified(before[target], after[target]))
        if args.mode == "edit":
            clean = not collateral and count_delta == 0
            print(f"{'PASS' if clean else 'FAIL'}  no collateral — "
                  f"{len(collateral)} other step(s) changed, {count_delta:+d} step(s) in length")
            ok &= clean
        else:
            drift = [(n, b, a) for n, b, a in collateral if _settings_in(b) != _settings_in(a)]
            print(f"INFO  structural mode — {len(collateral)} other step(s) reworded, "
                  f"{count_delta:+d} step(s) in length (allowed)")
            print(f"{'PASS' if not drift else 'FAIL'}  no silent number drift in the steps you "
                  f"did not mention — {len(drift)} drifted")
            ok &= not drift
            collateral = drift
        for n, b, a in collateral:
            print(f"  step {n} changed but was never mentioned:\n{_unified(b, a)}")

    print(f"\nconsole view (diff_plans, what the reviewer is shown): "
          f"dropped={len(prod['dropped'])} added={len(prod['added'])} "
          f"changed={[(c['param'], c['before'], c['after']) for c in prod['changed']]}")
    if not prod["dropped"] and not prod["added"] and collateral:
        print("⚠ FINDING: strict comparison sees changed steps that diff_plans reports as "
              "unchanged — the reviewer would not be told about them.")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("targets", help="list the editable numbers in a plan + what to ask for")
    t.add_argument("plan")
    t.set_defaults(fn=cmd_targets)

    g = sub.add_parser("grade", help="score the plan you got back against the one you started with")
    g.add_argument("before")
    g.add_argument("after")
    g.add_argument("--step", type=int, default=0, help="1-based step you asked to change")
    g.add_argument("--param", default="", help="parameter you asked to change, e.g. resolution")
    g.add_argument("--to", default="", help="value you asked for, e.g. 1.0")
    g.add_argument("--mode", choices=("edit", "unchanged", "structural", "redraft"),
                   default="edit",
                   help="edit = only the step you named may change; unchanged = nothing may change "
                        "(noise/question tests); structural = you asked to add/remove/move a step, "
                        "so other steps may be reworded but must not lose a setting, a tool or "
                        "themselves (\"redraft\" is the old name for this)")
    g.set_defaults(fn=cmd_grade)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
