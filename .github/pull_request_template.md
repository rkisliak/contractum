# Pull request

The title must follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)
(`type(scope): description`, for example `fix(sdk): keep contract files byte-exact in git`). It
becomes the squash commit subject on `main`. See `CONTRIBUTING.md`.

## Summary

What changes, and why.

## Docs

- [ ] Docs updated
- [ ] Docs not needed because: ...

No docs impact: end this description with a last paragraph `Docs-Impact: none — <reason>` (the
`docs-gate` check reads it, and the squash commit keeps it).

## Checks run

List the checks you ran (for example `uv run pytest tests/repo`, `zensical build --strict`).
