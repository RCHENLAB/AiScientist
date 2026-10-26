"""The plan's own-judgement disclosure, carried into the answer and the report.

Before any step runs, the PI lists the choices it made itself rather than taking them from the
protocol, the dataset profile or the request (``ResearchLab.self_sourced``). Until prod run
74873590ca7b (2026-10-23) that record reached only plan.md and run_state.json: the run's 14-gene
interferon set was the PI's own pick ("a team choice, not a named published signature"), and the
answer called it "a frozen 14-gene interferon-stimulated gene (ISG) set" with nothing to tell a
reader it was not a published signature.

Two parts. ``writer_note`` goes into the writer's prompt, so the Methods say "chosen for this run"
where they describe such a choice. ``add_section`` appends the record itself, verbatim, under one
heading, so it is in the answer and the report whether or not the writer followed the note.
"""

from __future__ import annotations

from typing import Callable

HEADING = "## Choices made for this run"


def disclosed(text: str) -> str:
    """The disclosure worth showing: ``''`` when there is none, or when the PI wrote that every
    choice came from the guidance and the profile ("none — …")."""
    body = " ".join(str(text or "").split())
    if not body or body.lower().startswith("none"):
        return ""
    return body


def writer_note(text: str) -> str:
    """The writer's instruction for a run whose plan disclosed choices of its own; ``''`` otherwise."""
    body = disclosed(text)
    if not body:
        return ""
    return (
        "CHOICES MADE FOR THIS RUN — before any step ran, the plan recorded these decisions as the "
        "system's own judgement, NOT taken from a protocol, a published source or the request:\n"
        f"{body}\n"
        "Do not present any of them as standard, published or protocol-mandated. Where the Methods "
        "or Results describe one (a gene set, a threshold, a cut-off, an added comparison), say in "
        "that sentence that it was chosen for this run, and name in the Limitations each one a "
        "result depends on. A section quoting this record is added after your report, so do not "
        "copy it as a section of your own.\n")


def section(text: str) -> str:
    """The section quoting the disclosure; ``''`` when there is nothing to disclose."""
    body = disclosed(text)
    if not body:
        return ""
    return (f"{HEADING}\n\n"
            "Before any step ran, the plan recorded these decisions as its own judgement, not taken "
            "from a protocol, a published source or the request:\n\n"
            f"> {body}\n")


def add_section(md: str, text: str, insert: "Callable[[str, str], str] | None" = None) -> str:
    """``md`` with the disclosure section, once: appended, or placed by ``insert(md, section)``.
    A text that already carries the heading (a writer that copied it from the answer) is left as
    it is."""
    block = section(text)
    if not block or HEADING in (md or ""):
        return md
    if insert is not None:
        return insert(md, block)
    return (md or "").rstrip() + "\n\n" + block
