# (Optional) Automation in the RetiGene repo

This file is **not needed to use the skill** — the methods are fully described in `SKILL.md` and
`publisher-access.md`, and work by hand or in any project.

For maintainers who also have the RetiGene codebase, the open rungs (1–8) and the ingest/verify
step are automated by Python scripts in the repo's `scripts/` directory (e.g. open-access API
recovery, PMC HTML/scan reconstruction, Europe PMC, Bookshelf, repository, and Internet Archive
imports, plus `_ingest.py` for verify-and-record). They implement exactly the methods documented
here; the skill itself does not depend on them.
