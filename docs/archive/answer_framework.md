# Answer Framework

This is the full research-answering framework for the proposed local multi-agent ocular biology assistant.

```mermaid
flowchart LR
    U["Researcher question"] --> I["Intake and safety check"]
    I --> C["Coordinator agent"]
    C --> M["Persistent workflow memory"]
    C --> L["Literature and context agent"]
    C --> D["Data and workflow agent"]
    D --> K["Kosmos skills and lab scripts"]
    D --> P["Local data preflight agent"]
    P --> Q["Single-cell QC execution agent"]
    Q --> E["Differential expression execution agent"]
    E --> H["HPC dry-run agent"]
    H --> S["Slurm dry-run or future sbatch"]
    S --> V["Validation agent"]
    V --> A["Research evaluation agent"]
    A --> O["Chat answer and report"]
    O --> M
    M --> C
```

The principle is simple: every answer should be backed by a recorded plan, separated execution responsibilities, an independent evaluation pass, and reusable memory.
