"""Model-written code: a deployment switch, off by default (RCHENLAB/AiScientist#150).

Every task rests on a tested tool or skill, or it is not offered. That holds for every account,
admins included. With ``AISCIENTIST_MODEL_WRITTEN_CODE`` off (the default):

* the code tools (``run_code``, ``run_shell``, ``run_in_environment``) and the tools that only
  serve them (``request_environment``, ``install_package``) are not offered to the model, and a call
  to one is refused at dispatch (``ResearchHarness``). Reading goes through the lookups
  (``inspect_dataset``, ``list_dir``, ``read_text``, ``find_files``, ``stat_path``, ...). The
  platform's own image runner, which runs the MCP servers' calls, is not a model tool and is not
  affected;
* a plan step that would need them is not accepted, whatever the plan came from. It is reported
  as not available, with the closest skill when there is one (``research_lab/code_gate.py``). A
  template skill (values marked ``ADAPT``) is named that way and does not run: filling it in is
  model-written code;
* skill induction, which learns skills from accepted ``run_code``, is off.

Turning the switch on restores the earlier behaviour: such code runs and its results are labelled
untested (``code_provenance.py``, issue 13).
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .plan_doc import CODE_TOOLS, split_step

ENV_VAR = "AISCIENTIST_MODEL_WRITTEN_CODE"

#: What the switch withholds: the tools that run code the model writes, and the two that exist only
#: to give that code software (a lab environment, a pip package).
WITHHELD_TOOLS = frozenset(CODE_TOOLS | {"request_environment", "install_package"})

#: What a template skill lacks, completing "It does not ...".
TEMPLATE_LACKS = ("run as published: its script is a template whose values the AI edits, and this "
                  "deployment does not run code the AI writes")


def model_written_code() -> bool:
    """Is model-written code allowed on this deployment? Off unless the operator turned it on."""
    return os.environ.get(ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def withheld_tools() -> frozenset[str]:
    """The tool names this deployment does not offer the model: none with the switch on."""
    return frozenset() if model_written_code() else WITHHELD_TOOLS


def offered(tools: Iterable[Any]) -> list[Any]:
    """``tools`` without the withheld ones."""
    withheld = withheld_tools()
    return [t for t in tools if getattr(t, "name", None) not in withheld]


def refusal(name: Any) -> "str | None":
    """Why a call to ``name`` is refused at dispatch, or ``None`` when it may run."""
    if name not in withheld_tools():
        return None
    return (f"'{name}' is turned off on this deployment: AiScientist runs only its tested tools, and "
            "never code the AI writes, not even to check something. Use one of the listed tools (the "
            "lookups inspect_dataset, list_dir, read_text, find_files and stat_path read files). If no "
            "listed tool does what this step needs, call `finish` and say so plainly.")


# --- not available: what the plan card and the reply say -------------------------------------------

def _plain(text: str) -> str:
    return " ".join(re.sub(r"[*`]", "", str(text or "")).split())


def step_name(step: str) -> str:
    """How a reply names a plan step: its title, else the start of its text."""
    title, does = split_step(step)
    name = _plain(title) or _plain(does)
    return name if len(name) <= 120 else name[:117].rstrip() + "..."


def skill_does(summary: str) -> str:
    """What a skill does, in the words of its description's first sentence, ready to follow a colon."""
    text = _plain(summary)
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].rstrip(".!? ")
    if len(first) > 240:
        first = first[:237].rstrip() + "..."
    return first[:1].lower() + first[1:] if first[:2] != first[:2].upper() else first


@dataclass(frozen=True)
class NotAvailable:
    """A plan step that does not run because no tested tool or skill does it.

    ``skill`` is the closest skill, ``does`` what that skill does and ``lacks`` what it does not do
    (completing "It does not ..."). ``needs`` is set instead when the step itself is covered but
    needs the output of another step that is not available: it names that step."""

    step: str
    skill: str = ""
    does: str = ""
    lacks: str = ""
    needs: str = ""

    @property
    def name(self) -> str:
        return step_name(self.step)

    def line(self, *, recorded: bool = True) -> str:
        """The sentence the plan card and the reply show for this step (#150's wording)."""
        if self.needs:
            return (f'**Not available:** "{self.name}" needs the output of "{step_name(self.needs)}", '
                    "which no tested tool or skill does, so it does not run either.")
        if self.skill:
            does = f": {self.does}" if self.does else ""
            return (f'**Not available:** no tested tool or skill does "{self.name}". The closest skill '
                    f"is `{self.skill}`{does}. It does not {self.lacks or TEMPLATE_LACKS}.")
        return (f'**Not available:** no tested tool or skill does "{self.name}", so we cannot do this '
                "yet." + (" The request has been recorded for the team." if recorded else ""))

    def to_dict(self) -> dict[str, Any]:
        return {**{k: v for k, v in asdict(self).items() if v}, "name": self.name}

    @classmethod
    def from_dict(cls, d: "dict[str, Any]") -> "NotAvailable":
        return cls(**{k: str(d.get(k) or "") for k in ("step", "skill", "does", "lacks", "needs")})


def not_available_markdown(items: "Iterable[NotAvailable | dict[str, Any]]", *, recorded: bool) -> str:
    """The reply's paragraph for the steps that did not run; '' when there are none."""
    rows = [i if isinstance(i, NotAvailable) else NotAvailable.from_dict(i) for i in items]
    if not rows:
        return ""
    return "\n\n".join(r.line(recorded=recorded) for r in rows) + "\n"


# --- what the planners are told ------------------------------------------------------------------

_PLANNING_NOTE = (
    "This deployment runs no code the AI writes: there is no run_code, run_shell or "
    "run_in_environment, and a skill whose script would have to be adapted cannot run. Plan every "
    "step with one of the listed tools. When the request asks for an analysis that none of them "
    "performs, still write that analysis as a step of its own, saying what it would do and naming no "
    "tool (name the closest skill in backticks if one fits): AiScientist tells the user that step is "
    "not available and runs the rest. Never add such a step that the request did not ask for.")


def planning_note() -> str:
    """What the PI and the plan review are told about code, or '' when code is allowed."""
    return "" if model_written_code() else _PLANNING_NOTE
