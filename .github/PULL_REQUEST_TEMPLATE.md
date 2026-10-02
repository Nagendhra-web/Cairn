## Summary

What does this change and why?

## Checklist

- [ ] Tests added or updated (unit, integration or e2e as appropriate)
- [ ] `ruff check`, `mypy` and `pytest` pass locally (`make check`)
- [ ] New nondeterministic operations go through `Recorder.effect` (replayable)
- [ ] New data sources assign provenance labels; new side-effecting tools declare effects and sensitive parameters
- [ ] Docs updated (`docs/`, README, CHANGELOG)
- [ ] No em dash characters (`python scripts/check_no_em_dash.py`)
