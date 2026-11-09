## Summary

Describe what this pull request changes and why.

## Scope

- [ ] Agent logic
- [ ] Tool execution
- [ ] Evaluation / harness
- [ ] Documentation
- [ ] CI / dependency management
- [ ] Other:

## Safety Boundary Checklist

- [ ] No `.env` file, API key, credential, or private dataset is committed.
- [ ] Raw expression rows are not sent to an LLM or external service.
- [ ] Literature/network calls use sanitized public queries only.
- [ ] Work on HPC3 ran as Slurm jobs under your own account: login nodes only logged in and
      submitted, and files moved through `access-hpc3.rcic.uci.edu`.
- [ ] Generated code, if added, cannot read private raw datasets or access the network by default.
- [ ] Local fallback outputs, if added, are labeled as diagnostic/preflight only.

## Validation

Paste the commands you ran:

```bash
python -m pytest
```

If the change touches Slurm jobs, SSH, staging or HPC3 storage, say what you ran on HPC3: the
job ids, the partition and the data.

## Notes for Reviewer

Call out any uncertain research assumptions, incomplete pieces, or expected follow-up work.
