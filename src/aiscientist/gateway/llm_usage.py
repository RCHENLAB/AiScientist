"""Token usage and estimated spend: the Account › Usage page, the chat and run counters, budgets.

Everything here reads ``llm_calls`` (one row per model call, written by
``auth_routes.record_llm_call``) and ``usage_budgets``. A user sees only their own rows; an admin
also sees totals per user, model and day, and nothing else — no key, prompt or answer is stored in
either table.

The dollar figures are ESTIMATES built from AiScientist's own record of its requests: the provider's
figure when it reports one (OpenRouter), else the configurable price table (:mod:`llm_pricing`).
The Claude Console is the authoritative source; it also sees the same key used outside AiScientist
and the other keys of the organization, which share the same credit.

Days and the billing cycle are counted in the viewer's time zone (``tz``, an IANA name the browser
sends), UTC when it sends none.
"""

from __future__ import annotations

import calendar
import csv
import datetime as _dt
import io
import statistics
from typing import Any, Iterable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter(prefix="/api")

CONSOLE_USAGE_URL = "https://platform.claude.com/usage"
ESTIMATE_NOTE = ("These figures are estimates from AiScientist's own record of its requests. The "
                 "Claude Console is the authoritative source for what your key spent and the credit "
                 "left: it also counts this key used outside AiScientist, and other keys of the same "
                 "Console organization, which share one credit balance.")
_TOKEN_KEYS = ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "total_tokens")


# --- time -----------------------------------------------------------------------------------------


def _zone(tz: str | None) -> _dt.tzinfo:
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - an unknown name falls back to UTC
            pass
    return _dt.timezone.utc


def _now(zone: _dt.tzinfo) -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).astimezone(zone)


def _local(ts: _dt.datetime | None, zone: _dt.tzinfo) -> _dt.datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:            # SQLite hands back naive UTC
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts.astimezone(zone)


def _day_in(year: int, month: int, day: int) -> _dt.date:
    return _dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def billing_cycle(today: _dt.date, reset_day: int) -> tuple[_dt.date, _dt.date]:
    """(first day, last day) of the cycle containing ``today`` for a cycle that restarts on
    ``reset_day`` of each month (the 31st means the last day of a shorter month)."""
    reset_day = max(1, min(31, int(reset_day or 1)))
    start = _day_in(today.year, today.month, reset_day)
    if today < start:
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start = _day_in(y, m, reset_day)
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start, _day_in(y, m, reset_day) - _dt.timedelta(days=1)


def _start_of(day: _dt.date, zone: _dt.tzinfo) -> _dt.datetime:
    """Midnight of ``day`` in ``zone``, as UTC (what the database compares against)."""
    return _dt.datetime.combine(day, _dt.time(), zone).astimezone(_dt.timezone.utc)


# --- reading rows ---------------------------------------------------------------------------------


def _rows(user_id: int | None, since: _dt.datetime | None = None, *,
          conversation_id: str | None = None, run_id: str | None = None) -> list:
    from sqlalchemy import select

    from .db import session_scope
    from .models import LlmCall
    stmt = select(LlmCall)
    if user_id is not None:
        stmt = stmt.where(LlmCall.user_id == user_id)
    if since is not None:
        stmt = stmt.where(LlmCall.created_at >= since)
    if conversation_id is not None:
        stmt = stmt.where(LlmCall.conversation_id == str(conversation_id))
    if run_id is not None:
        stmt = stmt.where(LlmCall.run_id == run_id)
    with session_scope() as s:
        rows = s.scalars(stmt.order_by(LlmCall.id)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def _tokens0() -> dict:
    return dict.fromkeys(_TOKEN_KEYS, 0)


def _empty() -> dict:
    # ``cluster``: the part of the calls and tokens that no API key paid for (the cluster GPU, which
    # HPC3 charges by GPU time, not by token). Every other figure counts all calls; the API-key share
    # is the whole minus this.
    return {"calls": 0, "errors": 0, **_tokens0(), "spend_usd": 0.0, "unpriced_calls": 0,
            "cluster": {"calls": 0, **_tokens0()}}


def _add(acc: dict, r) -> dict:
    acc["calls"] += 1
    if (r.status or "ok") == "error":
        acc["errors"] += 1
    cached = (r.cache_write_tokens or 0) + (r.cache_read_tokens or 0)
    tokens = {"input_tokens": r.input_tokens if r.input_tokens is not None else max(0, r.prompt_tokens - cached),
              "cache_write_tokens": r.cache_write_tokens or 0, "cache_read_tokens": r.cache_read_tokens or 0,
              "output_tokens": r.completion_tokens or 0, "total_tokens": r.total_tokens or 0}
    for k, v in tokens.items():
        acc[k] += v
    if not r.remote:
        acc["cluster"]["calls"] += 1
        for k, v in tokens.items():
            acc["cluster"][k] += v
    spend = r.spend_usd
    if spend is None:
        # A billed call the table has no price for. Calls on the cluster GPU are recorded at 0.
        if r.remote and (r.status or "ok") != "error":
            acc["unpriced_calls"] += 1
    else:
        acc["spend_usd"] += spend
    return acc


def totals(rows: Iterable) -> dict:
    acc = _empty()
    for r in rows:
        _add(acc, r)
    acc["spend_usd"] = round(acc["spend_usd"], 6)
    return acc


def _group(rows: Iterable, key) -> dict[Any, dict]:
    out: dict[Any, dict] = {}
    for r in rows:
        k = key(r)
        acc = out.setdefault(k, {**_empty(), "last_at": None})
        _add(acc, r)
        if r.created_at is not None and (acc["last_at"] is None or r.created_at > acc["last_at"]):
            acc["last_at"] = r.created_at
    for acc in out.values():
        acc["spend_usd"] = round(acc["spend_usd"], 6)
        if acc["last_at"] is not None:
            last = acc["last_at"]
            acc["last_at"] = (last if last.tzinfo else last.replace(tzinfo=_dt.timezone.utc)).isoformat()
    return out


def _ranked(groups: dict, name: str, limit: int = 50) -> list[dict]:
    rows = [{name: k, **v} for k, v in groups.items()]
    rows.sort(key=lambda x: (x["spend_usd"], x["total_tokens"]), reverse=True)
    return rows[:limit]


def _chat_titles(user_id: int, ids: Iterable[str]) -> dict[str, str]:
    from sqlalchemy import select

    from .db import session_scope
    from .models import Conversation
    nums = [int(i) for i in ids if i and str(i).isdigit()]
    if not nums:
        return {}
    with session_scope() as s:
        found = s.execute(select(Conversation.id, Conversation.title)
                          .where(Conversation.user_id == user_id, Conversation.id.in_(nums))).all()
    return {str(i): t for i, t in found}


def _run_info(user_id: int | None, ids: Iterable[str]) -> dict[str, dict]:
    from sqlalchemy import select

    from .db import session_scope
    from .models import Run
    ids = [i for i in ids if i]
    if not ids:
        return {}
    stmt = select(Run.run_id, Run.question, Run.status, Run.conversation_id).where(Run.run_id.in_(ids))
    if user_id is not None:
        stmt = stmt.where(Run.user_id == user_id)
    with session_scope() as s:
        found = s.execute(stmt).all()
    return {rid: {"question": (q or "")[:160], "status": st, "conversation_id": cid}
            for rid, q, st, cid in found}


# --- budgets --------------------------------------------------------------------------------------


def _budget_row(user_id: int):
    from .db import session_scope
    from .models import UsageBudget
    with session_scope() as s:
        row = s.get(UsageBudget, user_id)
        if row is not None:
            s.expunge(row)
        return row


def budget_status(user_id: int, zone: _dt.tzinfo, *, cycle_rows: list | None = None) -> dict:
    """The cycle, what was spent in it, and where that stands against the user's budget."""
    row = _budget_row(user_id)
    now = _now(zone)
    start, end = billing_cycle(now.date(), row.reset_day if row else 1)
    if cycle_rows is None:
        cycle_rows = _rows(user_id, _start_of(start, zone))
    spent = totals(cycle_rows)["spend_usd"]
    elapsed = max(1.0, (now - _start_of(start, zone).astimezone(zone)).total_seconds() / 86400)
    out: dict[str, Any] = {
        "cycle": {"start": start.isoformat(), "end": end.isoformat(),
                  "days_elapsed": round(elapsed, 2), "days_total": (end - start).days + 1},
        "spent_usd": spent,
        "budget": row.public() if row else None,
        "percent": None, "remaining_usd": None, "projected_exhaustion": None,
        "lasts_cycle": None, "crossed": [], "new_notices": [], "hard_limit_reached": False,
    }
    if row is None:
        return out
    credit = row.monthly_credit_usd
    if credit and credit > 0:
        pct = 100.0 * spent / credit
        out["percent"] = round(pct, 1)
        out["remaining_usd"] = round(credit - spent, 6)
        rate = spent / elapsed
        if credit - spent <= 0:
            out["projected_exhaustion"] = now.date().isoformat()
            out["lasts_cycle"] = False
        elif rate > 0:
            when = now + _dt.timedelta(days=(credit - spent) / rate)
            out["lasts_cycle"] = when.date() > end
            out["projected_exhaustion"] = None if out["lasts_cycle"] else when.date().isoformat()
        else:
            out["lasts_cycle"] = True
        crossed = [t for t in row.threshold_list() if pct >= t]
        shown = set()
        if row.notified_cycle == start.isoformat():
            shown = {int(x) for x in (row.notified or "").split(",") if x.strip().isdigit()}
        out["crossed"] = crossed
        out["new_notices"] = [t for t in crossed if t not in shown]
    if row.hard_limit_usd is not None and row.hard_limit_usd >= 0 and spent >= row.hard_limit_usd:
        out["hard_limit_reached"] = True
    return out


def budget_gate(user_id: int | None, tz: str | None = None) -> dict | None:
    """Why a NEW run on a metered endpoint should stop, or None.

    ``{"mode": "confirm" | "block", "message", "spent_usd", "hard_limit_usd"}`` once the cycle's
    estimated spend has reached the user's hard limit. Best-effort: an accounting failure never
    blocks a run."""
    if user_id is None:
        return None
    try:
        row = _budget_row(user_id)
        if row is None or row.hard_limit_usd is None:
            return None
        st = budget_status(user_id, _zone(tz))
        if not st["hard_limit_reached"]:
            return None
        mode = "block" if row.hard_limit_mode == "block" else "confirm"
        msg = (f"This billing cycle's estimated spend (${st['spent_usd']:.2f}) has reached the "
               f"${row.hard_limit_usd:.2f} limit you set under Account › Usage.")
        msg += (" New runs on your API key are blocked until the cycle resets on "
                f"{_next_day(st['cycle']['end'])} or you raise the limit."
                if mode == "block" else " Start this run anyway?")
        return {"mode": mode, "message": msg, "spent_usd": st["spent_usd"],
                "hard_limit_usd": row.hard_limit_usd}
    except Exception as exc:  # noqa: BLE001 - see the docstring
        print(f"[llm-usage] budget check skipped: {exc}")
        return None


def _next_day(iso: str) -> str:
    return (_dt.date.fromisoformat(iso) + _dt.timedelta(days=1)).isoformat()


def typical_run_cost(user_id: int | None, model: str, *, limit: int = 20) -> dict | None:
    """What this user's recent runs on ``model`` cost: ``{"runs", "median", "low", "high"}`` over
    the last ``limit`` finished runs whose calls mostly used it, or None with fewer than 2."""
    if user_id is None or not model:
        return None
    try:
        from sqlalchemy import select

        from .db import session_scope
        from .models import LlmCall, Run
        with session_scope() as s:
            # The user's most recent finished runs, then only their calls: not the whole history.
            recent = s.scalars(select(Run.run_id).where(Run.user_id == user_id,
                                                        Run.status.in_(("done", "incomplete")))
                               .order_by(Run.id.desc()).limit(limit * 3)).all()
            rows = s.scalars(select(LlmCall).where(LlmCall.user_id == user_id,
                                                   LlmCall.run_id.in_(recent or ["-"]),
                                                   LlmCall.remote.is_(True))).all()
            for r in rows:
                s.expunge(r)
        by_run: dict[str, dict] = {}
        for r in rows:
            slot = by_run.setdefault(r.run_id, {"spend": 0.0, "models": {}, "last": r.id})
            slot["spend"] += r.spend_usd or 0.0
            slot["models"][r.model] = slot["models"].get(r.model, 0) + (r.total_tokens or 0)
            slot["last"] = max(slot["last"], r.id)
        costs = [v["spend"] for v in sorted(by_run.values(), key=lambda v: v["last"], reverse=True)
                 if v["spend"] > 0 and max(v["models"], key=v["models"].get) == model][:limit]
        if len(costs) < 2:
            return None
        return {"runs": len(costs), "median": round(statistics.median(costs), 4),
                "low": round(min(costs), 4), "high": round(max(costs), 4)}
    except Exception as exc:  # noqa: BLE001 - an estimate is optional
        print(f"[llm-usage] run estimate skipped: {exc}")
        return None


def _sql_totals(user_id: int, *, conversation_id: str | None = None, run_id: str | None = None) -> dict:
    """Calls, tokens and spend of a chat or a run, summed in the database: this runs after every
    model call of a live run, and a long chat has thousands of rows. ``cluster`` is the part on the
    cluster GPU, as in ``_empty``."""
    from sqlalchemy import and_, case, func, or_, select

    from .db import session_scope
    from .models import LlmCall
    spend = func.coalesce(LlmCall.cost_usd, LlmCall.est_cost_usd)
    unpriced = and_(LlmCall.remote.is_(True), LlmCall.cost_usd.is_(None), LlmCall.est_cost_usd.is_(None),
                    or_(LlmCall.status.is_(None), LlmCall.status != "error"))
    on_cluster = or_(LlmCall.remote.is_(False), LlmCall.remote.is_(None))     # ``not r.remote`` in _add
    stmt = select(func.count(LlmCall.id), func.coalesce(func.sum(LlmCall.total_tokens), 0),
                  func.coalesce(func.sum(spend), 0.0),
                  func.coalesce(func.sum(case((unpriced, 1), else_=0)), 0),
                  func.coalesce(func.sum(case((on_cluster, 1), else_=0)), 0),
                  func.coalesce(func.sum(case((on_cluster, LlmCall.total_tokens), else_=0)), 0)
                  ).where(LlmCall.user_id == user_id)
    if conversation_id is not None:
        stmt = stmt.where(LlmCall.conversation_id == str(conversation_id))
    if run_id is not None:
        stmt = stmt.where(LlmCall.run_id == run_id)
    with session_scope() as s:
        calls, tokens, usd, unpriced_n, cluster_n, cluster_tokens = s.execute(stmt).one()
    return {"calls": int(calls), "total_tokens": int(tokens), "spend_usd": round(float(usd), 6),
            "unpriced_calls": int(unpriced_n),
            "cluster": {"calls": int(cluster_n), "total_tokens": int(cluster_tokens)}}


def live_totals(user_id: int | None, conversation_id: str | None, run_id: str | None) -> dict:
    """The chat's and the run's running totals, pushed after each recorded call."""
    out: dict[str, Any] = {}
    if user_id is None:
        return out
    if conversation_id:
        out["chat"] = _sql_totals(user_id, conversation_id=conversation_id)
    if run_id:
        out["run"] = {"run_id": run_id, **_sql_totals(user_id, run_id=run_id)}
    return out


# --- routes ---------------------------------------------------------------------------------------


def _user(request: Request):
    from . import auth_routes
    return auth_routes.require_user(request)


def _pricing() -> dict:
    from . import llm_pricing
    return llm_pricing.info()


def _period_start(period: str, zone: _dt.tzinfo, cycle_start: _dt.date) -> _dt.datetime | None:
    today = _now(zone).date()
    if period == "today":
        return _start_of(today, zone)
    if period == "7d":
        return _start_of(today - _dt.timedelta(days=6), zone)
    if period == "30d":
        return _start_of(today - _dt.timedelta(days=29), zone)
    if period == "all":
        return None
    return _start_of(cycle_start, zone)


@router.get("/usage/summary")
def usage_summary(request: Request, tz: str = "") -> dict:
    """The budget state the console checks after each run (threshold notices)."""
    user = _user(request)
    zone = _zone(tz)
    st = budget_status(user.id, zone)
    return {**st, "note": ESTIMATE_NOTE, "console_url": CONSOLE_USAGE_URL}


@router.get("/usage")
def usage_page(request: Request, tz: str = "", period: str = "cycle", days: int = 30) -> dict:
    """Everything the usage page shows."""
    user = _user(request)
    zone = _zone(tz)
    now = _now(zone)
    days = max(7, min(int(days or 30), 92))
    row = _budget_row(user.id)
    cycle_start, _ = billing_cycle(now.date(), row.reset_day if row else 1)
    chart_start = now.date() - _dt.timedelta(days=days - 1)
    period = period if period in ("today", "7d", "cycle", "30d", "all") else "cycle"
    since = _period_start(period, zone, cycle_start)
    earliest = min(_start_of(chart_start, zone), _start_of(cycle_start, zone),
                   _start_of(now.date() - _dt.timedelta(days=6), zone))
    if since is not None:
        earliest = min(earliest, since)
    rows = _rows(user.id, None if since is None else earliest)

    def after(t: _dt.datetime):
        return [r for r in rows if r.created_at is not None and _local(r.created_at, zone) >= t.astimezone(zone)]

    cycle_rows = after(_start_of(cycle_start, zone))
    picked = rows if since is None else after(since)
    daily = {}
    for r in after(_start_of(chart_start, zone)):
        d = _local(r.created_at, zone).date().isoformat()
        daily.setdefault(d, []).append(r)
    chart = []
    for i in range(days):
        d = (chart_start + _dt.timedelta(days=i)).isoformat()
        t = totals(daily.get(d, []))
        chart.append({"date": d, "spend_usd": t["spend_usd"], "total_tokens": t["total_tokens"],
                      "calls": t["calls"]})

    by_chat = _ranked(_group([r for r in picked if r.conversation_id], lambda r: r.conversation_id),
                      "conversation_id")
    titles = _chat_titles(user.id, [c["conversation_id"] for c in by_chat])
    for c in by_chat:
        c["title"] = titles.get(str(c["conversation_id"]))
    by_run = _ranked(_group([r for r in picked if r.run_id], lambda r: r.run_id), "run_id")
    info = _run_info(user.id, [x["run_id"] for x in by_run])
    for x in by_run:
        x.update(info.get(x["run_id"]) or {})
    errors = _ranked(_group([r for r in picked if (r.status or "ok") == "error"],
                            lambda r: r.error_type or "error"), "error_type")
    return {
        "period": period,
        "totals": {"today": totals(after(_start_of(now.date(), zone))),
                   "last_7_days": totals(after(_start_of(now.date() - _dt.timedelta(days=6), zone))),
                   "cycle": totals(cycle_rows),
                   "period": totals(picked)},
        "budget": budget_status(user.id, zone, cycle_rows=cycle_rows),
        "daily": chart,
        "by_model": _ranked(_group(picked, lambda r: r.model or "(unknown)"), "model"),
        "by_chat": by_chat,
        "by_run": by_run,
        "errors": errors,
        "pricing": _pricing(),
        "note": ESTIMATE_NOTE,
        "console_url": CONSOLE_USAGE_URL,
    }


@router.get("/usage/export.csv")
def usage_export(request: Request, tz: str = "", period: str = "cycle") -> Response:
    """One line per model call in the period, for a spreadsheet."""
    user = _user(request)
    zone = _zone(tz)
    row = _budget_row(user.id)
    cycle_start, _ = billing_cycle(_now(zone).date(), row.reset_day if row else 1)
    period = period if period in ("today", "7d", "cycle", "30d", "all") else "cycle"
    rows = _rows(user.id, _period_start(period, zone, cycle_start))
    titles = _chat_titles(user.id, {r.conversation_id for r in rows if r.conversation_id})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_local", "model", "role", "step", "chat_id", "chat_title", "run_id", "provider",
                "billed_to_api_key", "input_tokens", "cache_write_tokens", "cache_read_tokens",
                "output_tokens", "total_tokens", "provider_cost_usd", "estimated_cost_usd", "status",
                "error_type", "duration_ms"])
    for r in rows:
        cached = (r.cache_write_tokens or 0) + (r.cache_read_tokens or 0)
        w.writerow([
            (_local(r.created_at, zone) or "").isoformat() if r.created_at else "", r.model, r.role,
            r.step or "", r.conversation_id or "", titles.get(str(r.conversation_id or ""), ""),
            r.run_id or "", r.provider or "", "yes" if r.remote else "no",
            r.input_tokens if r.input_tokens is not None else max(0, (r.prompt_tokens or 0) - cached),
            r.cache_write_tokens or 0, r.cache_read_tokens or 0, r.completion_tokens or 0,
            r.total_tokens or 0, "" if r.cost_usd is None else f"{r.cost_usd:.6f}",
            "" if r.est_cost_usd is None else f"{r.est_cost_usd:.6f}", r.status or "ok",
            r.error_type or "", r.duration_ms or 0])
    name = f"aiscientist-usage-{period}-{_now(zone).date().isoformat()}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/usage/chat/{conversation_id}")
def usage_for_chat(conversation_id: str, request: Request) -> dict:
    user = _user(request)
    return {"conversation_id": conversation_id, **_sql_totals(user.id, conversation_id=conversation_id)}


@router.get("/usage/run/{run_id}")
def usage_for_run(run_id: str, request: Request) -> dict:
    """A run's totals, and its breakdown by pipeline step and by model."""
    user = _user(request)
    rows = _rows(user.id, run_id=run_id)
    steps: list[dict] = []
    for name, acc in _group(rows, lambda r: r.step or "Run setup").items():
        first = min(r.id for r in rows if (r.step or "Run setup") == name)
        steps.append({"step": name, **acc, "_first": first})
    steps.sort(key=lambda x: x.pop("_first"))
    errors = _ranked(_group([r for r in rows if (r.status or "ok") == "error"],
                            lambda r: r.error_type or "error"), "error_type")
    return {"run_id": run_id, **totals(rows), "by_step": steps,
            "by_model": _ranked(_group(rows, lambda r: r.model or "(unknown)"), "model"),
            "by_role": _ranked(_group(rows, lambda r: (r.role or "reason").split(":")[0]), "role"),
            "errors": errors, "note": ESTIMATE_NOTE}


class BudgetRequest(BaseModel):
    monthly_credit_usd: float | None = None
    reset_day: int = 1
    thresholds: list[int] | None = None
    hard_limit_usd: float | None = None
    hard_limit_mode: str = "confirm"


@router.get("/usage/budget")
def get_budget(request: Request) -> dict:
    user = _user(request)
    row = _budget_row(user.id)
    return {"budget": row.public() if row else None}


@router.put("/usage/budget")
def put_budget(req: BudgetRequest, request: Request) -> dict:
    from .db import session_scope
    from .models import UsageBudget
    user = _user(request)
    for name, val in (("monthly credit", req.monthly_credit_usd), ("hard limit", req.hard_limit_usd)):
        if val is not None and not (0 <= val <= 1_000_000):
            raise HTTPException(status_code=400, detail=f"The {name} must be between $0 and $1,000,000.")
    if not 1 <= int(req.reset_day) <= 31:
        raise HTTPException(status_code=400, detail="The reset day must be a day of the month (1-31).")
    if req.hard_limit_mode not in ("confirm", "block"):
        raise HTTPException(status_code=400, detail="The hard limit is either 'confirm' or 'block'.")
    marks = sorted({int(t) for t in (req.thresholds if req.thresholds is not None else [50, 80, 100])
                    if 0 < int(t) <= 1000})
    with session_scope() as s:
        row = s.get(UsageBudget, user.id)
        if row is None:
            row = UsageBudget(user_id=user.id)
            s.add(row)
        reset_changed = row.reset_day != int(req.reset_day)
        row.monthly_credit_usd = req.monthly_credit_usd
        row.reset_day = int(req.reset_day)
        row.thresholds = ",".join(str(t) for t in marks)
        row.hard_limit_usd = req.hard_limit_usd
        row.hard_limit_mode = req.hard_limit_mode
        if reset_changed:
            row.notified_cycle, row.notified = None, None
        s.commit()
        return {"budget": row.public()}


class NoticeAck(BaseModel):
    thresholds: list[int]
    tz: str = ""


@router.post("/usage/notices/ack")
def ack_notices(req: NoticeAck, request: Request) -> dict:
    """Mark threshold notices as shown for the current cycle, so each appears once."""
    from .db import session_scope
    from .models import UsageBudget
    user = _user(request)
    zone = _zone(req.tz)
    with session_scope() as s:
        row = s.get(UsageBudget, user.id)
        if row is None:
            return {"status": "ok"}
        start, _ = billing_cycle(_now(zone).date(), row.reset_day)
        shown = set()
        if row.notified_cycle == start.isoformat():
            shown = {int(x) for x in (row.notified or "").split(",") if x.strip().isdigit()}
        shown |= {int(t) for t in req.thresholds}
        row.notified_cycle = start.isoformat()
        row.notified = ",".join(str(t) for t in sorted(shown))
        s.commit()
    return {"status": "ok"}


@router.get("/admin/usage")
def admin_usage(request: Request, tz: str = "", days: int = 30) -> dict:
    """Usage across all accounts by user, model and day. No key, prompt or answer is in these
    rows; an admin sees counts and estimated dollars only."""
    from sqlalchemy import select

    from . import auth_routes
    from .db import session_scope
    from .models import User
    auth_routes.require_admin(request)
    zone = _zone(tz)
    days = max(1, min(int(days or 30), 366))
    start = _now(zone).date() - _dt.timedelta(days=days - 1)
    rows = _rows(None, _start_of(start, zone))
    with session_scope() as s:
        names = dict(s.execute(select(User.id, User.username)).all())
    by_user = _ranked(_group(rows, lambda r: r.user_id), "user_id", limit=500)
    for u in by_user:
        u["username"] = names.get(u["user_id"]) or ("(no account)" if u["user_id"] is None else "(deleted)")
    by_day = _group(rows, lambda r: _local(r.created_at, zone).date().isoformat() if r.created_at else "")
    every_day = [(start + _dt.timedelta(days=i)).isoformat() for i in range(days)]
    return {"days": days, "since": start.isoformat(), "totals": totals(rows), "by_user": by_user,
            "by_model": _ranked(_group(rows, lambda r: r.model or "(unknown)"), "model", limit=200),
            # Every day of the period, the quiet ones at 0, so a chart of it keeps the calendar.
            "by_day": [{"date": d, **by_day.get(d, {**_empty(), "last_at": None})} for d in every_day],
            "pricing": _pricing(), "note": ESTIMATE_NOTE}
