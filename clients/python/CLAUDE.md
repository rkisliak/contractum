# clients/python (contractum SDK and CLI)

The read-only data-engineer client for Contractum: a Python library and a `contractum` command
line that fetch published contracts, pin and cache them, and test local data against them.

It is **not** a member of the server uv workspace. It has its own `pyproject.toml` and `uv.lock`;
run every command from `clients/python`. The root `pyproject.toml` excludes `clients/*` from the
workspace and root ruff, so the server gates (lock, lint, coverage) never see these files.

## What this module owns

- The `contractum` distribution (`src/contractum/`): the closed error taxonomy (`errors.py`), the
  lazy click CLI (`cli/`), the HTTP transport, auth and token handling, the profile/config store,
  lock and cache, the fetch client, and the local test engine, source readers and report writers.
- `src/contractum/_generated/`: vendored output of the API client generator.
- Its own tests (`tests/unit`, `tests/integration`), its own CI (`.github/workflows/sdk.yml`) and
  its own coverage figure.

## What it must never import

- No server code: `dc_core`, `dc_auth`, `dc_odcs`, `dc_rules`, `dc_identity`, `dc_contracts`,
  `dc_validation`, `dc_audit`, `dc_mail`, nor `fastapi`, `sqlalchemy`, `alembic`. Enforced by
  import-linter (D-05) with a planted-violation test.
- The engine and report modules stay free of `click`, `rich` and any network code; only the CLI
  layer touches click and rich, only the transport and auth layer touches the network.
- `_generated/` is imported only by `_api.py`.

## Rules

- Licence: Apache-2.0 (`LICENSE` is the unmodified official text, the copyright line is in
  `NOTICE`; D-46). Never copy the repository's noncommercial server licence into this package.
- Dependencies use version ranges, never exact pins (D-03). `ruff` is the one dev exception and
  stays pinned so formatting is stable.
- Read-only towards Contractum: GET (and HEAD) only, no POST at all. Enforced in `_transport.py`
  and by tests (`tests/unit/test_write_fence_cli.py` asserts zero POSTs across every command).
- Sign-in is a token only (D-36): `contractum login --token -` (or a hidden prompt on a terminal)
  verifies the token with `GET /api/v1/identity/me` and stores it; `CONTRACTUM_TOKEN` is used as
  is. No password, browser, device-code or client-credentials sign-in, no Entra public client.
- `_generated/` is vendored generator output pinned to openapi-python-client 0.30.0 (D-45). Its
  `httpx2` references are rewritten to `httpx` by the generator script (the SDK keeps one HTTP
  stack). Never edit it by hand; regenerate it and commit the result.
- Secrets are never printed, logged or written into reports. Token examples use only the shape
  `dcp_<public_id>_<secret>`, never a value. Tests use the canary token and `assert_no_secret`.
- Exit codes are 0 (success), 1 (data does not match the contract) and 2 (usage or internal
  error, every `ContractumError`). Unexpected exceptions exit 2 and print only the type name.
- Python 3.11 floor (D-37): stdlib `tomllib`, `datetime.UTC` and `typing.Self` are fine; nothing
  newer than 3.11 (no `type` statements, no PEP 695 generics). CI runs 3.11 to 3.14, plus Windows,
  macOS, lowest-direct dependencies on 3.11 and the newest allowed releases on 3.14.
- Coverage floor is 98 percent, measured on unit plus integration runs combined (D-48). The
  unit-only CI job is an extra next to it; never present a unit-only figure as the SDK figure.
- Mark tests `unit` (no network, no server) or `integration` (needs the stack or a container).

## Key files and entry points

- `pyproject.toml`: ranges, extras (`pandas`, `polars`, `delta`, `sql`, `all`), ruff,
  mypy strict, pytest markers, coverage and import-linter config.
- `src/contractum/cli/__init__.py`: `LazyGroup`, the `COMMANDS` map (add a command by creating the
  module it names) and the `main()` exit-code wrapper.
- `src/contractum/errors.py`: the closed `ContractumError` hierarchy.
- `tests/conftest.py`: canary token, `assert_no_secret`, path-based markers, isolated
  config/data/cache environment.
- `../../.github/workflows/sdk.yml`: SDK CI, aggregated by the `sdk-ok` check.
- `../../.github/workflows/sdk-release.yml`: release path. Pull requests dry-run the build and
  smoke; an `contractum-v*` tag publishes wheel, sdist and `SHA256SUMS` as a GitHub Release (`gh release
  create`, attested) and pushes `ghcr.io/<owner>/contractum-cli`. No package index (D-42).

## Module skills

- `.claude/skills/add-check/SKILL.md`: teach the test engine a new ODCS check (a `CheckSpec` in
  `engine/plan.py` plus a kernel in `engine/kernels.py`, or a `Skipped` reason; never report an
  unevaluated rule as passed).
- `.claude/skills/regenerate-client/SKILL.md`: regenerate the vendored API client after an API change
  (`openapi/operations.txt`, `scripts/generate_client.py`, the fence test; never edit `_generated/`).

## Commands

Run from `clients/python`:

```bash
uv sync --all-extras                                   # install with the lock
uv run pytest -m unit -q                               # unit tests
uv run coverage erase && uv run coverage run -m pytest -q && uv run coverage combine \
  && uv run coverage report --fail-under=98            # coverage (unit plus integration for D-48)
uv run lint-imports --config pyproject.toml            # server-import fence
uv run mypy                                            # strict typing
uv run ruff check . && uv run ruff format --check .    # lint and format
```
