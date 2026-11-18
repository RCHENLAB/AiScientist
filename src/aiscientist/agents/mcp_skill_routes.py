"""Skills paired with an MCP server, as candidates of the protocol router.

The router (``preset_pipelines.select_pipeline``) picks one protocol for a request before the plan
is drafted. Its candidates were the preset pipelines alone. A skill of the pinned skills release
that is paired with an MCP server was only a line in the tool list, so a request it fits was
steered by the nearest preset instead: production run 6bc2b3b32969 ("check expression pattern of
RPE65 genes in this dataset", on a shared atlas file) was routed to gene-signature scoring and
planned as model-written code, with the atlas expression tools registered and usable.

A paired skill is a candidate when:

- no preset pipeline already composes one of its server's tools (those requests have a protocol);
- at least one of the server's tools is usable in this session;
- the attached dataset is one the server accepts (the atlas server reads listed atlases only).

Its guidance to the PI is short and built here: the skill's description, the server's tools with
the first sentence of each description, and the rule to plan with those tools, not with code.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping

from .preset_pipelines import PresetPipeline

# metadata.category of a skill -> the data modality the router's content routing knows.
_CATEGORY_DATA_TYPE = {"single-cell": "scrna", "variants": "variants"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _first_sentences(text: str, limit: int) -> str:
    """Whole leading sentences of ``text`` up to ``limit`` characters; at least the first one."""
    out = ""
    for sentence in _SENTENCE.split(" ".join((text or "").split())):
        if out and len(out) + 1 + len(sentence) > limit:
            break
        out = f"{out} {sentence}".strip()
    return out


def _accepts(server, dataset_paths: Iterable[str]) -> bool:
    """Would the server's adapter take the attached dataset? Only the atlas server is particular:
    it reads an atlas the deployment lists, or a file of the shared-datasets store."""
    if server.adapter != "atlas-expression":
        return True
    from . import atlas_expression as atlas
    import posixpath
    listed = atlas.configured_atlases()
    for path in dataset_paths:
        path = str(path or "")
        if path.endswith(".h5ad") and (posixpath.normpath(path) in listed or atlas.shared_release(path)):
            return True
    return False


def skill_pipelines(
    library: Iterable[PresetPipeline],
    available_tools: "frozenset[str] | None",
    tool_descriptions: Mapping[str, str],
    dataset_paths: Iterable[str] = (),
) -> tuple[PresetPipeline, ...]:
    """The paired skills to add to the router's candidates, in registry order. Empty when the
    session's tools are unknown (``available_tools`` is None): a candidate is never offered on a
    guess that its tools can run."""
    if available_tools is None:
        return ()
    from .mcp_bridge import configured_servers
    from .skills import get_skill

    covered = {tool for pipeline in library for tool in pipeline.tools}
    taken = {pipeline.key for pipeline in library}
    paths = tuple(dataset_paths)
    out: list[PresetPipeline] = []
    try:
        servers = configured_servers()
    except Exception:  # noqa: BLE001 - a registry that cannot be read adds no candidate
        return ()
    for server in servers:
        names = [server.planner_name(tool["name"]) for tool in server.tools]
        usable = [name for name in names if name in available_tools]
        if not usable or covered.intersection(names) or server.skill in taken:
            continue
        skill = get_skill(server.skill)
        if skill is None or not _accepts(server, paths):
            continue
        tool_lines = "\n".join(
            f"- `{name}`: {_first_sentences(tool_descriptions.get(name, ''), 220)}".rstrip(": ")
            for name in usable)
        prompt = (
            f"Protocol from the skill `{skill.name}` (MCP server `{server.name}`).\n"
            f"{_first_sentences(skill.summary, 700)}\n\n"
            "Plan this request with the server's typed tools below. They are tested and they are the "
            "only way to run this analysis: do not plan `run_code` for what one of them does, and do "
            "not add quality control, clustering or other analyses the request does not ask for.\n"
            f"{tool_lines}\n\n"
            "Plan only the steps the request needs, in the order the tool descriptions give. For a "
            f"choice they do not settle, read the skill's guidance with "
            f"`read_skill_reference(\"{skill.name}\")`.")
        out.append(PresetPipeline(
            key=skill.name,
            label=_first_sentences(skill.summary, 320),
            prompt=prompt,
            tools=tuple(usable),
            data_type=_CATEGORY_DATA_TYPE.get(skill.category, ""),
            title=skill.name,
            summary=_first_sentences(skill.summary, 160)))
    return tuple(out)
