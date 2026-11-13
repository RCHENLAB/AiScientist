"""Small VCF header detector used by uploads independently of annotation tools."""
from __future__ import annotations

import re

# chr1 lengths distinguish GRCh37/hg19 from GRCh38/hg38, so it disambiguates even when the ``##reference``/``assembly=`` tags are
# absent or wrong. Used to pick the matching VEP cache: annotating hg19 coordinates against a GRCh38
# cache runs cleanly but mis-assigns the gene at ~90% of sites (empirically verified on a real WGS VCF).
_CHR1_LEN_TO_BUILD: dict[str, str] = {"249250621": "GRCh37", "248956422": "GRCh38"}


def detect_assembly(header_text: str) -> str:
    """Best-effort genome build (``'GRCh37'`` | ``'GRCh38'`` | ``''``) from a VCF header. Prefers the
    chr1 contig length (unambiguous across builds), then falls back to explicit ``assembly=`` /
    ``##reference`` tags. Returns ``''`` when unknown so the caller keeps its configured default."""
    m = re.search(r"##contig=<ID=(?:chr)?1,length=(\d+)", header_text)
    if m and m.group(1) in _CHR1_LEN_TO_BUILD:
        return _CHR1_LEN_TO_BUILD[m.group(1)]
    low = header_text.lower()
    if any(tag in low for tag in ("grch37", "hg19", "b37")):
        return "GRCh37"
    if any(tag in low for tag in ("grch38", "hg38", "b38")):
        return "GRCh38"
    return ""
