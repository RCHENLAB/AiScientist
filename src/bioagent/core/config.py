from __future__ import annotations

import os
import re
from pathlib import Path

# --- brand env-var migration (BioAgent -> AiScientist) ------------------------
# The project rebranded to AiScientist, but the code still reads ~1000 ``BIOAGENT_*`` env
# vars and the prod ``.env`` is full of them. Renaming the env keys outright would break every
# deployed ``.env`` the instant the new code ships. Instead we alias the two prefixes at startup:
# ops can set EITHER ``AISCIENTIST_X`` or ``BIOAGENT_X`` and both the old (``os.environ["BIOAGENT_X"]``)
# and new (``os.environ["AISCIENTIST_X"]``) reads see the same value. This is the zero-downtime safety
# net for a future full rename: flip keys to ``AISCIENTIST_*`` in ``.env`` at your own pace; nothing
# breaks meanwhile. When both forms are set for a key, the NEW ``AISCIENTIST_*`` value wins.
_OLD_PREFIX = "BIOAGENT_"
_NEW_PREFIX = "AISCIENTIST_"


def apply_brand_env_aliases(env: "os._Environ[str] | dict[str, str] | None" = None) -> None:
    """Mirror ``BIOAGENT_*`` <-> ``AISCIENTIST_*`` env vars so either prefix works (new wins). Idempotent
    and safe to call repeatedly. Operates on ``os.environ`` unless a mapping is passed (for tests)."""
    target = os.environ if env is None else env
    # Base names (the part after the prefix) that appear under either brand prefix.
    bases: set[str] = set()
    for key in list(target.keys()):
        if key.startswith(_NEW_PREFIX) and len(key) > len(_NEW_PREFIX):
            bases.add(key[len(_NEW_PREFIX):])
        elif key.startswith(_OLD_PREFIX) and len(key) > len(_OLD_PREFIX):
            bases.add(key[len(_OLD_PREFIX):])
    for base in bases:
        new_key, old_key = _NEW_PREFIX + base, _OLD_PREFIX + base
        # Canonical value: the new-brand key wins when both are set; else whichever exists.
        if new_key in target:
            value = target[new_key]
        else:
            value = target[old_key]
        target[new_key] = value
        target[old_key] = value


def env(name: str, default: str | None = None) -> str | None:
    """Read a brand env var by its BASE name (no prefix) — the ``AISCIENTIST_*`` form first, then the
    legacy ``BIOAGENT_*`` form. Convenience for NEW code so it doesn't hard-code either prefix; existing
    ``os.environ.get("BIOAGENT_X")`` call sites keep working via :func:`apply_brand_env_aliases`."""
    base = name
    for prefix in (_NEW_PREFIX, _OLD_PREFIX):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    val = os.environ.get(_NEW_PREFIX + base)
    if val is None:
        val = os.environ.get(_OLD_PREFIX + base)
    return default if val is None else val


# An inline "# comment" after an UNQUOTED value (the `#` must be preceded by whitespace, so a
# '#' that is part of the value itself — e.g. inside a URL or password — is left alone).
_INLINE_COMMENT = re.compile(r"\s+#.*$")


def _clean_value(raw: str) -> str:
    """Normalise a .env value: drop surrounding whitespace, one layer of matching quotes, and an
    inline ``# comment``. A QUOTED value keeps its inner text verbatim (so a ``#`` inside quotes
    survives); only an UNQUOTED value has its trailing `` # …`` stripped. This matters because
    systemd's ``EnvironmentFile=`` does NOT strip inline comments, so a line like
    ``BIOAGENT_VLLM_MAX_MODEL_LEN=131072   # 80GB A100`` otherwise reaches the process as the whole
    string ``131072   # 80GB A100`` — which ``settings._int`` can't parse and silently falls back to
    the default (a real prod bug: 128K requested, 32K served)."""
    v = raw.strip()
    if v[:1] in ("'", '"'):
        quote = v[0]
        end = v.find(quote, 1)
        return v[1:end] if end != -1 else v[1:]
    return _INLINE_COMMENT.sub("", v).strip()


def load_dotenv(path: Path) -> dict[str, str]:
    """Load simple KEY=VALUE pairs from a .env file, stripping inline ``# comment``s from values.

    Existing environment values are respected (a real external override wins), EXCEPT a value that
    differs from the .env value ONLY by such an inline comment — that is systemd's ``EnvironmentFile``
    having loaded the SAME line un-stripped, so we repair it to the clean value. Without this repair
    the app's own load is a no-op (``setdefault``) and the polluted systemd copy wins."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = _clean_value(raw_value)
        loaded[key] = value
        existing = os.environ.get(key)
        if existing is None or (existing != value and _clean_value(existing) == value):
            os.environ[key] = value
    return loaded


def load_project_env(start: Path | None = None) -> dict[str, str]:
    base = start or Path.cwd()
    loaded = load_dotenv(base / ".env")
    # Mirror the brand prefixes AFTER the .env is loaded, so a .env that sets EITHER
    # AISCIENTIST_* or BIOAGENT_* satisfies every read of the other form.
    apply_brand_env_aliases()
    return loaded
