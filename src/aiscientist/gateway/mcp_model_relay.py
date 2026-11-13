"""The session's own model for an MCP server whose job sends its model calls back here.

An MCP tool call is one CPU job on HPC3 (``agents/mcp_bridge``). A server that reads with a model, such as
retigene-rag's curation, must read with the model of the user's session, and that model is reachable only
from the gateway: a user's API key stays in the key service, the lab's shared model sits behind the
gateway's tunnel and its queue, the session's GPU behind the session's tunnel. So the job writes each
prompt to a relay folder on dfs3b and :class:`gateway.llm_relay.ModelRelay` answers it, for as long as
the job runs, with the session's lab-role completion (``_lab_llm(conn).complete_fn``, role ``curation``).
That function records each call against the user's usage, checks the key's guard and waits at the shared
model's gate, so nothing here records a call a second time.

``Binding`` is what one call reads with, resolved when the call is prepared: the model's name and prices
(for the server's own estimate), the spending cap on a user's key (what is left of their budget) and the
spike allowance announced for the key while the job runs.
"""

from __future__ import annotations

import contextlib
import posixpath
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .llm_relay import ModelRelay, RelayFS, RelayStats

#: The lab role a relayed curation prompt is answered under (``anthropic_client._ROLE_EFFORT``: low).
ROLE = "curation"


@dataclass(frozen=True)
class Binding:
    """The session's model for one MCP call.

    ``complete(messages, *, role, max_tokens, usage_out) -> text`` is the session's lab-role completion.
    ``metered``: a user's own API key (priced, on their bill); False for the lab's shared model and the
    session's GPU. ``rates``: USD per million input / output tokens, for the server's estimate.
    ``cap``: USD this call may spend (what is left of the user's budget), None when unpriced.
    ``price(usage) -> USD`` prices one reply. ``allowance(usd)`` announces the job's spending to the key's
    spike check and returns something with ``close()``; None when there is no key."""

    complete: Callable[..., str]
    model: str
    metered: bool
    rates: tuple[float, float] = (0.0, 0.0)
    cap: float | None = None
    price: Callable[[dict[str, Any]], float | None] = lambda usage: 0.0
    allowance: Callable[[float], Any] | None = None


class SessionModelRelay:
    """What an MCP adapter is handed (as the executor's ``model_relay``) to read with the session's model.

    ``bind()`` resolves the session's model, or raises when there is none to read with; ``fs_factory()``
    opens the relay folder's file system (SFTP on the transfer host); ``work_dir`` is the run's work
    folder on HPC3, bound read-write into the job, under which each call gets a folder of its own."""

    def __init__(self, bind: Callable[[], Binding], fs_factory: Callable[[], RelayFS], work_dir: str, *,
                 should_stop: Callable[[], bool] | None = None, log: Callable[[str], None] = print,
                 concurrency: int = 16, poll_s: float = 1.0) -> None:
        self._bind, self.fs_factory, self.work_dir = bind, fs_factory, work_dir.rstrip("/")
        self.should_stop, self.log = should_stop, log
        self.concurrency, self.poll_s = concurrency, poll_s

    def open(self, name: str = "mcp") -> "RelayCall":
        """A relay for one tool call. Raises ``ValueError`` (the bridge's refusal type) with the reason
        when the session has no model to read with, or no budget left on its key."""
        try:
            binding = self._bind()
        except Exception as exc:  # noqa: BLE001 - a GatewayError's message is written for the user
            if isinstance(exc, ValueError):
                raise
            raise ValueError(str(getattr(exc, "message", "") or exc)) from exc
        folder = posixpath.join(self.work_dir, f"{name}-relay", uuid.uuid4().hex[:16])
        return RelayCall(self, binding, folder)


class RelayCall:
    """One tool call's relay: its folder, the model it reads with, and :meth:`serving` around the job."""

    def __init__(self, owner: SessionModelRelay, binding: Binding, folder: str) -> None:
        self.owner, self.binding, self.folder = owner, binding, folder
        self.stats: RelayStats | None = None

    def _call(self, messages: list[dict[str, Any]], max_tokens: int) -> tuple[str, dict[str, Any]]:
        usage: dict[str, Any] = {}
        text = self.binding.complete(messages, role=ROLE, max_tokens=max_tokens, usage_out=usage)
        return text, usage

    @contextlib.contextmanager
    def serving(self) -> Iterator[ModelRelay]:
        """Answer the job's prompts while the ``with`` body runs the job, then stop."""
        b = self.binding
        held = b.allowance(float(b.cap or 0)) if b.allowance is not None else None
        relay = ModelRelay(self.owner.fs_factory, self.folder, self._call, price=b.price, record=None,
                           max_cost_usd=b.cap, concurrency=self.owner.concurrency, poll_s=self.owner.poll_s,
                           should_stop=self.owner.should_stop, log=self.owner.log)
        relay.start()
        try:
            yield relay
        finally:
            self.stats = relay.stop()
            if held is not None:
                held.close()
