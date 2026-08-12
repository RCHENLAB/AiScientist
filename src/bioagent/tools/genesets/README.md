# Gene-set libraries (GMT) for offline enrichment

`run_enrichment` reads local `.gmt` files from here (or from `$BIOAGENT_GENESETS_DIR`)
instead of calling the Enrichr web API, so enrichment stays fast and reproducible and does not
depend on network access (the analysis container now allows network on-demand, but local ORA
avoids the web API's rate limits and version drift).

The `.gmt` files are **not** committed (large, license-varied). Download them once:

    python scripts/fetch_genesets.py            # -> this dir, default 3 libraries
    python scripts/fetch_genesets.py /dfs3b/.../src/bioagent/tools/genesets   # for the HPC3 source bind

Defaults (freely redistributable): GO_Biological_Process_2023, Reactome_2022,
MSigDB_Hallmark_2020. KEGG is not downloaded by default (redistribution is
license-restricted) — pass `--libs KEGG_2021_Human` only if cleared for your use.
