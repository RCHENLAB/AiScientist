"""DRAFT — the multi-agent lab kernel: a small FIXED dispatcher + DATA-driven registries.

Proposed for the week-of-2026-06-22 discussion (HANDOFF "2026-06-18 (later)"). NOT wired
into the gateway. Pure Python — no LangGraph/LangChain. The LLM is injected as
``complete_fn(messages) -> str``, so the whole thing is offline-testable
(``tests/test_lab_kernel_draft.py``).

THE ANTI-BLOAT POINT
--------------------
The dispatcher is a small FIXED kernel — three primitives (:meth:`Lab.call_tool`,
:meth:`Lab.run_agent_turn`, :meth:`Lab.run_meeting`) plus a PI plan/route loop. New
WORKFLOWS are DATA, not code:

- a :class:`Tool`    = a deterministic capability (scanpy / scGPT job / CodeAct / ...),
- an :class:`Agent`  = an expert persona with its OWN context + scoped tools,
- a :class:`Playbook`= a steering prompt + which experts/tools a workflow convenes.

Adding a workflow registers a Playbook (+ maybe a Tool/Agent) — it never edits the kernel.
So the dispatch protocol does not grow with the number of workflows.

AUTONOMY = "PI outputs -> dispatcher executes" (handoff). Each step the PI emits the NEXT
action as JSON ({"action": "meeting|agent|tool|finish", ...}); the kernel executes it,
checkpoints, and asks again. (A Playbook can make this as scripted or as free as you like —
the kernel doesn't care.)

Two distinct axes (keep them separate so it stays clean):
- sub-AGENT = independent expert *mind* (perspective / judgement) — convene for thinking;
- TOOL      = deterministic *capability* — call to get something computed. A MISSING tool is
  a capability gap -> CodeAct, NOT a reason to spawn an agent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .archive import LabArchive

# complete_fn(messages) -> assistant text. Injected so tests need no GPU/LLM.
CompleteFn = Callable[[list[dict[str, str]]], str]
# tool executor: (args, ctx) -> result dict
ToolExec = Callable[[dict[str, Any], "LabContext"], dict[str, Any]]


# --------------------------------------------------------------------------- data
@dataclass(frozen=True)
class Tool:
    """A deterministic capability (NOT an agent). Mirrors the gateway's HarnessTool shape."""
    name: str
    description: str
    executor: ToolExec
    category: str = "general"
    requires: tuple[str, ...] = ()
    reads_private_data: bool = False


@dataclass(frozen=True)
class Agent:
    """An expert sub-agent: an independent persona with its OWN memory + scoped tools."""
    id: str
    title: str
    expertise: str
    goal: str
    role: str = ""
    tools: tuple[str, ...] = ()          # tool names this agent may call (scoped)
    model: str | None = None             # optional per-agent model override

    def system_prompt(self) -> str:
        extra = f" {self.role}" if self.role else ""
        return (f"You are the {self.title}. Expertise: {self.expertise}. Goal: {self.goal}.{extra} "
                "Ground every claim in tool outputs; never invent numbers or gene symbols.")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "expertise": self.expertise,
                "goal": self.goal, "tools": list(self.tools), "model": self.model}


@dataclass(frozen=True)
class Playbook:
    """A workflow expressed as DATA: a steering prompt + which experts/tools it convenes."""
    key: str
    label: str
    guidance: str                        # steers the PI's planning + routing
    experts: tuple[str, ...] = ()        # agent ids to convene (empty = whole roster)
    tools: tuple[str, ...] = ()          # scoped tool names (empty = all)


class Registry:
    """Tiny name -> item store; the data-driven half of the design."""
    def __init__(self) -> None:
        self._items: dict[str, Any] = {}

    def register(self, item: Any) -> Any:
        self._items[item.key if isinstance(item, Playbook) else item.id if isinstance(item, Agent) else item.name] = item
        return item

    def get(self, key: str) -> Any:
        return self._items.get(key)

    def names(self) -> list[str]:
        return list(self._items)

    def all(self) -> list[Any]:
        return list(self._items.values())


# --------------------------------------------------------------------------- config / ctx
@dataclass
class LabConfig:
    max_steps: int = 12            # PI route steps before forced synthesis
    meeting_rounds: int = 2        # turn-taking rounds per team meeting
    max_tool_turns: int = 4        # tool calls one agent may chain in a turn


@dataclass
class LabContext:
    complete_fn: CompleteFn
    archive: LabArchive
    config: LabConfig = field(default_factory=LabConfig)
    artifacts_dir: str = ""


@dataclass
class LabResult:
    question: str
    agenda: list[str]
    steps_taken: int
    final_answer: str
    status: str


# --------------------------------------------------------------------------- prompts
PI_PLAN = ("You are the Principal Investigator. Plan 2-6 ordered, concrete agenda steps "
           "achievable with the listed experts and tools. Reply with ONLY a JSON array of strings.")
PI_ROUTE = ("You are the Principal Investigator running the project. Given the agenda and "
            "progress, choose the NEXT action. Reply with ONLY a JSON object, one of: "
            '{"action":"meeting","participants":[ids],"agenda":str,"rounds":int} | '
            '{"action":"agent","agent":id,"task":str} | '
            '{"action":"tool","tool":name,"args":{}} | '
            '{"action":"finish","answer":str}. Use finish only when the agenda is complete.')
PI_SYNTH = ("You are the Principal Investigator writing the final answer, grounded ONLY in "
            "the progress provided. State limitations honestly; frame conclusions as hypotheses.")
CRITIC = ("You are a rigorous Scientific Critic. Point out what is weak, missing, or "
          "unsupported in the discussion so far. Be specific and constructive.")


def _loads(raw: str) -> Any:
    """Tolerant JSON parse (strips code fences / surrounding prose). None on failure."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[4:].strip() if s.lower().startswith("json") else s
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# --------------------------------------------------------------------------- the kernel
class Lab:
    """The fixed dispatcher. Holds registries + the injected LLM; drives one project."""

    def __init__(self, complete_fn: CompleteFn, archive: LabArchive, *,
                 tools: Registry | None = None, agents: Registry | None = None,
                 playbooks: Registry | None = None, config: LabConfig | None = None,
                 should_cancel: Callable[[], bool] | None = None) -> None:
        self.tools = tools or Registry()
        self.agents = agents or Registry()
        self.playbooks = playbooks or Registry()
        self.config = config or LabConfig()
        self.should_cancel = should_cancel
        self.ctx = LabContext(complete_fn=complete_fn, archive=archive, config=self.config,
                              artifacts_dir=str(archive.artifacts_dir()))
        self.archive = archive

    # -- helpers --------------------------------------------------------------
    def _complete(self, system: str, user: str) -> str:
        return self.ctx.complete_fn([{"role": "system", "content": system},
                                     {"role": "user", "content": user}])

    def _roster(self, pb: Playbook | None) -> list[Agent]:
        if pb and pb.experts:
            return [a for a in (self.agents.get(i) for i in pb.experts) if a]
        return self.agents.all()

    # -- primitive 1: call a deterministic tool -------------------------------
    def call_tool(self, name: str, args: dict[str, Any], by: str = "PI") -> dict[str, Any]:
        tool: Tool | None = self.tools.get(name)
        if tool is None:
            res = {"status": "error", "error": f"unknown tool {name}"}
        else:
            try:
                res = tool.executor(args or {}, self.ctx)
            except Exception as exc:  # noqa: BLE001 - tool failure is reported, never fatal
                res = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        self.archive.append_event("tool_call", tool=name, by=by, status=res.get("status", "ok"))
        return res

    # -- primitive 2: one expert agent thinks (own context), may call its tools
    def run_agent_turn(self, agent: Agent, task: str) -> str:
        msgs: list[dict[str, str]] = [{"role": "system", "content": agent.system_prompt()}]
        msgs += self.archive.read_memory(agent.id)          # this agent's OWN prior context
        msgs.append({"role": "user", "content": task})
        self.archive.append_memory(agent.id, "user", task)
        for _ in range(self.config.max_tool_turns + 1):
            reply = self.ctx.complete_fn(msgs)
            self.archive.append_memory(agent.id, "assistant", reply)
            d = _loads(reply)
            if isinstance(d, dict) and d.get("tool"):
                if d["tool"] not in agent.tools:
                    obs = f"TOOL_DENIED {d['tool']} is not in your scoped tools {list(agent.tools)}"
                else:
                    obs = f"TOOL_RESULT {d['tool']}: {json.dumps(self.call_tool(d['tool'], d.get('args', {}), by=agent.id))[:1500]}"
                msgs += [{"role": "assistant", "content": reply}, {"role": "user", "content": obs}]
                self.archive.append_memory(agent.id, "user", obs)
                continue
            return (d.get("final") if isinstance(d, dict) else None) or reply
        return "(agent did not converge)"

    # -- primitive 3: a team meeting (within-meeting shared discussion) --------
    def run_meeting(self, meeting_id: str, participants: list[Agent], agenda: str,
                    rounds: int | None = None) -> str:
        rounds = rounds or self.config.meeting_rounds
        self.archive.write_meeting(meeting_id, {"participants": [a.id for a in participants], "agenda": agenda})
        transcript: list[str] = []

        def render() -> str:
            return "\n".join(transcript) or "(no discussion yet)"

        for r in range(rounds):
            for agent in participants:
                user = f"Agenda: {agenda}\n\nDiscussion so far:\n{render()}\n\nAdd your point as the {agent.title}."
                reply = self._complete(agent.system_prompt(), user)
                transcript.append(f"{agent.title}: {reply}")
                self.archive.append_turn(meeting_id, agent.title, reply, round=r + 1)
            crit = self._complete(CRITIC, f"Agenda: {agenda}\n\nDiscussion:\n{render()}\n\nWhat is weak or missing?")
            transcript.append(f"Critic: {crit}")
            self.archive.append_turn(meeting_id, "Critic", crit, round=r + 1)
        summary = self._complete(PI_SYNTH, f"Agenda: {agenda}\n\nFull discussion:\n{render()}\n\nSynthesize the decision.")
        self.archive.write_summary(meeting_id, summary)
        self.archive.append_event("meeting", meeting_id=meeting_id, rounds=rounds)
        return summary

    # -- PI roles -------------------------------------------------------------
    def _pi_plan(self, question: str, pb: Playbook | None) -> list[str]:
        guidance = f"\nResearch-path guidance:\n{pb.guidance}\n" if pb else ""
        experts = "; ".join(f"{a.id}:{a.title}" for a in self._roster(pb)) or "(none)"
        user = (f"Question: {question}\n{guidance}\nExperts: {experts}\n"
                f"Tools: {', '.join(self.tools.names()) or '(none)'}\n\nReturn the agenda JSON array.")
        agenda = _loads(self._complete(PI_PLAN, user))
        return [str(x) for x in agenda] if isinstance(agenda, list) and agenda else [question]

    def _pi_next(self, question: str, pb: Playbook | None, state: dict[str, Any]) -> dict[str, Any]:
        experts = "; ".join(f"{a.id}:{a.title}" for a in self._roster(pb)) or "(none)"
        hist = "\n".join(f"- {h['action']}: {h['summary']}" for h in state["history"]) or "(nothing yet)"
        user = (f"Question: {question}\nAgenda: {state['agenda']}\nExperts: {experts}\n"
                f"Tools: {', '.join(self.tools.names()) or '(none)'}\nProgress:\n{hist}\n\nReturn the next action JSON.")
        d = _loads(self._complete(PI_ROUTE, user))
        return d if isinstance(d, dict) and d.get("action") else {"action": "finish", "answer": ""}

    def _synthesize(self, question: str, state: dict[str, Any]) -> str:
        hist = "\n".join(f"- {h['action']}: {h['summary']}" for h in state["history"]) or "(no steps completed)"
        return self._complete(PI_SYNTH, f"Question: {question}\nProgress (ground ONLY in this):\n{hist}\n\nWrite the final answer.")

    # -- dispatch one PI action ----------------------------------------------
    def _dispatch(self, d: dict[str, Any], step: int) -> str:
        action = d["action"]
        if action == "tool":
            return f"tool {d.get('tool')} -> {json.dumps(self.call_tool(d.get('tool', ''), d.get('args', {})))[:300]}"
        if action == "agent":
            agent = self.agents.get(d.get("agent", ""))
            if not agent:
                return f"unknown agent {d.get('agent')}"
            return f"{agent.title}: {self.run_agent_turn(agent, d.get('task', ''))[:300]}"
        if action == "meeting":
            parts = [a for a in (self.agents.get(i) for i in d.get("participants", [])) if a] or self.agents.all()
            return f"meeting -> {self.run_meeting(f'm{step}', parts, d.get('agenda', ''), d.get('rounds'))[:300]}"
        return f"(unknown action {action})"

    # -- the run loop (resumable) --------------------------------------------
    def run(self, question: str, playbook_key: str | None = None) -> LabResult:
        pb: Playbook | None = self.playbooks.get(playbook_key) if playbook_key else None
        resumed = self.archive.latest_checkpoint()
        if resumed:
            step, state = resumed[0] + 1, resumed[1]               # rehydrate + continue
            self.archive.append_event("resumed", from_step=resumed[0])
        else:
            self.archive.init_manifest(question)
            agenda = self._pi_plan(question, pb)
            self.archive.write_agenda(agenda)
            self.archive.write_team([a.as_dict() for a in self._roster(pb)])
            state = {"question": question, "agenda": agenda, "history": [], "final": None}
            step = 0
        self.archive.set_status("running")

        while step < self.config.max_steps:
            if self.should_cancel and self.should_cancel():
                self.archive.set_status("cancelled")
                state["final"] = state.get("final") or self._synthesize(question, state)
                break
            d = self._pi_next(question, pb, state)
            if d["action"] == "finish":
                state["final"] = d.get("answer") or self._synthesize(question, state)
                self.archive.append_event("finish")
                break
            summary = self._dispatch(d, step)
            state["history"].append({"action": d["action"], "summary": summary[:500]})
            self.archive.write_checkpoint(step, state)            # checkpoint EVERY step
            step += 1
        else:
            state["final"] = state.get("final") or self._synthesize(question, state)

        self.archive.set_status("done")
        return LabResult(question, state["agenda"], len(state["history"]),
                         state.get("final") or "", "done")
