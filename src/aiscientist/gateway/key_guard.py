"""Checks around every model call paid by a user's API key (RCHENLAB/AiScientist issue 16).

A run reads its key once, at its start (``_lab_llm``), and keeps it for its whole length, so a key
rotated mid-run cannot break the run. That is also why stopping a key needs a check BETWEEN calls:
deleting or rotating it does not reach a run that already holds it. :class:`KeyUse` is that check,
one per run (or chat turn, or corpus scan) that uses a key.

Before each call (:meth:`KeyUse.before_call`) the call is refused when

* **the key is paused** — by its owner, an admin, or the spike detector below
  (``llm_credentials.set_paused``);
* **every key was revoked** after this one entered the server (``aiscientist-admin
  purge-llm-keys`` writes the marker; ``llm_credentials.revoked_after``);
* **the user's hard limit is reached** (Account › API usage). Until now the limit was checked only
  when a run started. Now the cycle's spend is summed again between calls, from the calls recorded
  so far: in "block" mode the run stops; in "confirm" mode the user is asked once per run (the
  same card as the agent's other confirmations), unless they already chose to go on past the limit
  when the run started. The spend is still AiScientist's estimate (the price table, or the
  provider's figure when it reports one), and a call is checked before it is sent, not priced in
  advance: one call can still cross the limit.

After each call (:meth:`KeyUse.after_call`) the **spike check** compares the key's last hour with its
own busiest hour of the previous 14 days. A spike — more than ``FACTOR`` times that hour and above
an absolute floor — pauses the key (``AISCIENTIST_KEY_SPIKE_ACTION=notify`` only reports it) and
tells the owner: in the console, and by email when the account has an address. Only usage through
AiScientist is visible here; a key copied and used elsewhere shows only in the provider's console.

No function here returns, logs or stores a key: a ``KeyUse`` knows the credential's id and its
fingerprint (a hash prefix), never the secret.
"""

from __future__ import annotations

import datetime as _dt
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import GatewayError

# --- spike thresholds (env, read per check so an admin can tune without a restart) ----------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def spike_settings() -> dict[str, Any]:
    """The spike rule as configured. A key's last ``window_min`` minutes are a spike when their calls
    or their spend exceed BOTH the floor (plus what running capped jobs announced) AND ``factor``
    times the key's busiest hour of the previous ``history_days`` days."""
    return {
        "window_min": max(5.0, _env_float("AISCIENTIST_KEY_SPIKE_WINDOW_MIN", 60.0)),
        "min_calls": max(1.0, _env_float("AISCIENTIST_KEY_SPIKE_MIN_CALLS", 1000.0)),
        "min_usd": max(0.01, _env_float("AISCIENTIST_KEY_SPIKE_MIN_USD", 20.0)),
        "factor": max(1.0, _env_float("AISCIENTIST_KEY_SPIKE_FACTOR", 3.0)),
        "history_days": max(1.0, _env_float("AISCIENTIST_KEY_SPIKE_HISTORY_DAYS", 14.0)),
        "check_every_s": max(0.0, _env_float("AISCIENTIST_KEY_SPIKE_CHECK_S", 60.0)),
        "action": "notify" if os.environ.get("AISCIENTIST_KEY_SPIKE_ACTION", "pause").strip().lower()
        == "notify" else "pause",
    }


# Jobs that announced their own spending cap (the corpus scan): while one runs, its cap is added to
# the floor for its key, so a scan the user started and capped is not taken for misuse. Each lapses
# after ``_ALLOWANCE_TTL_S`` even if the job never said it ended.
_ALLOWANCES: dict[str, dict[int, tuple[float, float, float]]] = {}
_ALLOWANCE_TTL_S = 12 * 3600
_ALLOW_LOCK = threading.Lock()
# When each key was last checked for a spike, and when it last raised one (one alert per window).
_LAST_CHECK: dict[str, float] = {}
_LAST_ALERT: dict[str, float] = {}

# Callables the gateway installs (``install``): notify(owner, payload) pushes to the owner's open
# consoles; confirm is per KeyUse. Kept as hooks so this module needs no import of ``app``.
_NOTIFY: Callable[[str, dict], None] | None = None


def install(*, notify: Callable[[str, dict], None] | None = None) -> None:
    global _NOTIFY
    _NOTIFY = notify


def _allowance(fp: str) -> tuple[float, float]:
    now = time.monotonic()
    with _ALLOW_LOCK:
        held = _ALLOWANCES.get(fp, {})
        for key in [k for k, v in held.items() if v[2] < now]:
            held.pop(key, None)
        return sum(v[0] for v in held.values()), sum(v[1] for v in held.values())


@dataclass
class KeyUse:
    """One run's (or chat turn's, or scan's) use of one credential.

    ``confirm(summary, detail) -> bool`` asks the person watching (the in-run budget question);
    None where nobody can be asked mid-call (then "confirm" mode refuses and says to send again).
    ``budget_ack`` — the user already chose, at the start, to run past the hard limit.
    ``allowance_usd``/``allowance_calls`` — what a capped job may legitimately spend in the spike
    window (the corpus scan's cap)."""

    owner: str
    cred_id: str
    fingerprint: str
    label: str
    user_id: int | None = None
    entered_at: float = field(default_factory=time.time)
    confirm: Callable[[str, str], bool | None] | None = None
    budget_ack: Callable[[], bool] | bool = False
    tz: Callable[[], str | None] | str | None = None
    allowance_usd: float = 0.0
    allowance_calls: float = 0.0
    _over_limit_ok: bool = False
    _budget_cache: tuple[float, dict] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # One budget question at a time: a run's parallel steps can reach the limit together.
    _ask_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.allowance_usd or self.allowance_calls:
            with _ALLOW_LOCK:
                _ALLOWANCES.setdefault(self.fingerprint, {})[id(self)] = (
                    float(self.allowance_usd), float(self.allowance_calls),
                    time.monotonic() + _ALLOWANCE_TTL_S)

    def close(self) -> None:
        """The job ended: its spike allowance ends with it."""
        with _ALLOW_LOCK:
            held = _ALLOWANCES.get(self.fingerprint, {})
            held.pop(id(self), None)
            if not held:
                _ALLOWANCES.pop(self.fingerprint, None)

    # --- before a call ---------------------------------------------------------------------------

    def before_call(self) -> None:
        """Raise ``GatewayError`` when this call must not be made (see the module docstring)."""
        from . import llm_credentials
        revoked = llm_credentials.revoked_after()
        if revoked is not None and self.entered_at <= revoked:
            raise GatewayError(
                "An administrator revoked every API key saved on AiScientist, so this run stopped "
                "before its next model call. Rotate your key at the provider, then add it again "
                "under API endpoints.", stage="llm_key_revoked")
        row = llm_credentials.get_credential(self.owner, self.cred_id)
        if row is not None:
            llm_credentials.touch(self.owner, self.cred_id)
            if row.get("paused_at"):
                raise GatewayError(_paused_message(row, self.label), stage="llm_key_paused")
        self._check_budget()

    def _check_budget(self) -> None:
        if self.user_id is None or self._over_limit_ok:
            return
        # The sum is kept until the next recorded call (after_call) or for 2 s: calls made side by
        # side (a DAG's parallel steps, the corpus scan's 16) need not each sum the cycle again.
        now = time.monotonic()
        with self._lock:
            cached = self._budget_cache
        if cached is not None and now - cached[0] < 2.0:
            st = cached[1]
        else:
            try:
                from . import llm_usage
                st = llm_usage.cycle_spend(self.user_id, _value(self.tz))
            except Exception as exc:  # noqa: BLE001 - accounting never stops a run by itself failing
                print(f"[key-guard] budget check skipped: {exc}")
                return
            with self._lock:
                self._budget_cache = (now, st)
        if not st.get("reached"):
            return
        spent, limit = st["spent_usd"], st["hard_limit_usd"]
        where = (f"This billing cycle's estimated spend (${spent:.2f}) has reached the ${limit:.2f} "
                 "hard limit you set under Account › API usage")
        if st["mode"] == "block":
            raise GatewayError(
                f"{where}, so this run stopped before its next model call. Raise the limit, or wait "
                f"for the cycle to reset after {st['cycle_end']}.", stage="budget")
        if _value(self.budget_ack):
            self._over_limit_ok = True
            return
        answer = None
        if self.confirm is not None:
            with self._ask_lock:
                if self._over_limit_ok:          # a parallel step asked while this one waited
                    return
                answer = self.confirm(f"{where}. Continue this run on your key?",
                                      f"Estimated spend ${spent:.2f} of a ${limit:.2f} limit.")
                if answer:
                    self._over_limit_ok = True
        if answer:
            return
        raise GatewayError(
            f"{where}, so this {'run stopped' if self.confirm is not None else 'reply stopped'} "
            "before its next model call."
            + ("" if self.confirm is not None else " Send it again to be asked whether to go on."),
            stage="budget")

    # --- after a call ----------------------------------------------------------------------------

    def after_call(self) -> dict | None:
        """After a call was recorded: the next budget check sums again (the spend just changed),
        and the spike check (throttled per key). Returns the spike it found, or None. Never raises:
        a failed check must not fail the call that just succeeded."""
        with self._lock:
            self._budget_cache = None
        try:
            return check_spike(self)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            print(f"[key-guard] spike check skipped: {exc}")
            return None


def _value(v: Any) -> Any:
    return v() if callable(v) else v


def _paused_message(row: dict, label: str) -> str:
    by = str(row.get("paused_by") or "")
    who = ("an administrator" if by.startswith("admin")
           else "AiScientist, because its usage spiked" if by == "auto" else "you")
    why = f" ({row['pause_reason']})" if row.get("pause_reason") and by != "owner" else ""
    end = ("Ask an administrator to resume it." if by.startswith("admin")
           else "Resume it under API endpoints when you are sure the usage is yours.")
    return (f"The API key “{row.get('label') or label}” is paused by {who}{why}, so this run "
            f"stopped before its next model call. {end}")


# --- spike detection -----------------------------------------------------------------------------


def check_spike(use: KeyUse, *, now: _dt.datetime | None = None, force: bool = False) -> dict | None:
    """Compare the key's last window with its own history; on a spike pause it (or only report it)
    and notify the owner. Returns ``{"calls", "spend_usd", "baseline", "threshold", "action"}`` for
    a spike, else None."""
    s = spike_settings()
    fp = use.fingerprint
    mono = time.monotonic()
    if not force:
        last = _LAST_CHECK.get(fp)
        if last is not None and mono - last < s["check_every_s"]:
            return None
    _LAST_CHECK[fp] = mono
    from . import llm_usage
    now = now or _dt.datetime.now(_dt.timezone.utc)
    since = now - _dt.timedelta(minutes=s["window_min"])
    cur = llm_usage.key_window(fp, since)
    extra_usd, extra_calls = _allowance(fp)
    floor_calls, floor_usd = s["min_calls"] + extra_calls, s["min_usd"] + extra_usd
    if cur["calls"] <= floor_calls and cur["spend_usd"] <= floor_usd:
        return None                       # the cheap query settles almost every call
    base = llm_usage.key_busiest_hour(fp, since - _dt.timedelta(days=s["history_days"]), since)
    # A window longer than an hour is compared with that many busiest hours.
    hours = s["window_min"] / 60.0
    lim_calls = max(floor_calls, s["factor"] * base["calls"] * hours)
    lim_usd = max(floor_usd, s["factor"] * base["spend_usd"] * hours)
    if cur["calls"] <= lim_calls and cur["spend_usd"] <= lim_usd:
        return None
    last_alert = _LAST_ALERT.get(fp)
    if last_alert is not None and mono - last_alert < s["window_min"] * 60:
        return None                       # already reported for this window
    _LAST_ALERT[fp] = mono
    spike = {"calls": cur["calls"], "spend_usd": cur["spend_usd"], "window_min": s["window_min"],
             "baseline": base, "threshold": {"calls": round(lim_calls), "spend_usd": round(lim_usd, 2)},
             "action": s["action"]}
    reason = (f"{cur['calls']} calls and about ${cur['spend_usd']:.2f} in the last "
              f"{int(s['window_min'])} minutes; its busiest hour in the previous "
              f"{int(s['history_days'])} days was {base['calls']} calls and ${base['spend_usd']:.2f}")
    from . import llm_credentials
    paused = []
    if s["action"] == "pause":
        # Every credential of this owner that holds the same key: the spike is the key's.
        for owner, row in _owner_rows_with(use.owner, fp):
            if not row.get("paused_at"):
                llm_credentials.set_paused(owner, row["id"], True, by="auto", reason=f"usage spike: {reason}")
                paused.append(row["id"])
    print(f"[key-guard] usage spike on {use.owner}'s key {fp} ({use.label}): {reason}; "
          f"{'paused ' + ', '.join(paused) if paused else 'reported only'}")
    notify_owner(use.owner, use.user_id, kind="spike", label=use.label, reason=reason,
                 paused=bool(paused) or s["action"] == "pause", cred_ids=paused or [use.cred_id])
    return spike


def _owner_rows_with(owner: str, fp: str) -> list[tuple[str, dict]]:
    from . import llm_credentials
    return [(owner, r) for r in llm_credentials.owner_credentials(owner) if r.get("key_fingerprint") == fp]


# --- telling people ------------------------------------------------------------------------------


def notify_owner(owner: str, user_id: int | None, *, kind: str, label: str, reason: str = "",
                 paused: bool = True, cred_ids: list[str] | None = None, by: str = "") -> dict:
    """Tell a key's owner it was paused (or that its usage spiked): in every console they have open,
    and by email when their account has an address. ``kind`` is "spike" or "admin". Never raises.
    Returns what was done, for the caller's log and the tests."""
    done = {"pushed": False, "emailed": False}
    payload = {"type": "key_paused" if paused else "key_spike", "credential_ids": cred_ids or [],
               "label": label, "kind": kind, "reason": reason}
    try:
        if _NOTIFY is not None:
            _NOTIFY(owner, payload)
            done["pushed"] = True
    except Exception as exc:  # noqa: BLE001 - a notice never fails the caller
        print(f"[key-guard] console notice not sent: {exc}")
    email = _account_email(user_id, owner)
    if email:
        subject, body = _email_text(kind=kind, label=label, reason=reason, paused=paused, by=by)
        try:
            from . import email_send
            done["emailed"] = bool(email_send.send_email(email, subject, body))
        except Exception as exc:  # noqa: BLE001
            print(f"[key-guard] email to the key's owner not sent: {exc}")
    alert_to = [a.strip() for a in os.environ.get("AISCIENTIST_KEY_ALERT_EMAIL", "").split(",") if a.strip()]
    if alert_to and kind == "spike":
        from . import email_send
        for addr in alert_to:
            try:
                email_send.send_email(addr, f"AiScientist: usage spike on {owner}'s API key",
                                      f"Account: {owner}\nKey: {label}\n{reason}\n"
                                      f"Action: {'paused' if paused else 'reported only'}\n")
            except Exception as exc:  # noqa: BLE001
                print(f"[key-guard] admin alert not sent: {exc}")
    return done


def _account_email(user_id: int | None, owner: str) -> str | None:
    try:
        from sqlalchemy import select

        from .db import session_scope
        from .models import User
        with session_scope() as s:
            if user_id is not None:
                user = s.get(User, user_id)
            else:
                user = s.scalars(select(User).where(User.username == owner)).first()
            return (user.email or None) if user is not None and user.is_active else None
    except Exception:  # noqa: BLE001 - no accounts database: nobody to email
        return None


def _email_text(*, kind: str, label: str, reason: str, paused: bool, by: str) -> tuple[str, str]:
    if kind == "admin":
        subject = f"AiScientist: an administrator paused your API key “{label}”"
        what = (f"An administrator paused your API key “{label}” on AiScientist"
                + (f": {reason}" if reason else "") + ".")
        then = ("No model call uses it until an administrator resumes it. If you did not expect this, "
                "reply to your administrator.")
    else:
        subject = (f"AiScientist paused your API key “{label}”: unusual usage" if paused
                   else f"AiScientist: unusual usage of your API key “{label}”")
        what = f"Your API key “{label}” was used much more than usual through AiScientist: {reason}."
        then = ("AiScientist paused it, so no run uses it until you resume it under API endpoints. "
                if paused else "")
    body = (f"{what}\n\n{then}\n\n"
            "If this usage was not yours, rotate the key in your provider's console now (for Claude: "
            "platform.claude.com, API keys) and check its usage there; that console also shows any use "
            "of the key outside AiScientist. Then replace the key under API endpoints.\n\n"
            "If it was yours, resume the key under API endpoints.\n\n"
            "This message never contains your key.\n")
    return subject, body


#: The ``GatewayError`` stages of a call refused here, before anything was sent.
REFUSALS = frozenset({"llm_key_paused", "llm_key_revoked", "budget"})
