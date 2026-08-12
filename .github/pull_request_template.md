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
- [ ] Slurm submission remains dry-run or human-review gated.
- [ ] Generated code, if added, cannot read private raw datasets or access the network by default.
- [ ] Local fallback outputs, if added, are labeled as diagnostic/preflight only.

## Validation

Paste the commands you ran:

```bash
python -m pytest
```

## Notes for Reviewer

Call out any uncertain research assumptions, incomplete pieces, or expected follow-up work.
