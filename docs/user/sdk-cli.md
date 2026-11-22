# Contractum SDK and CLI for data engineers

For data engineers and pipeline owners who fetch published Contractum contracts and test their own data against them, from the command line, from Python, from pytest or from CI. The client is read-only: it never writes to Contractum.

The package is `contractum` (source in `clients/python`, licensed under the Apache License 2.0, see `clients/python/LICENSE`; the Contractum server keeps its own licence). It is published on the **GitHub Releases page of the Contractum repository** (tags `contractum-vX.Y.Z`), not on a package index. **No `contractum-v*` release has been published yet**: until the first tag is pushed, install from a checkout (`uv tool install ./clients/python`) or build the image yourself (see "Install"). The repository is `rkisliak/contractum`. In the commands below, `0.1.0` stands for the release you pin.

For every command and option, see the generated [CLI reference](reference/cli.md). For every public Python name, see the [SDK API reference](reference/sdk-api.md).

Contents: [Install](#install) | [Sign in](#sign-in) | [Configure](#configure) | [Find contracts](#find-contracts) | [Pull and lock](#pull-and-lock) | [Test data](#test-data) | [Reports and exit codes](#reports-and-exit-codes) | [Preview and compare](#preview-and-compare) | [pytest](#pytest-plugin) | [Library use](#library-use) | [CI recipes](#ci-recipes) | [Security](#security) | [Troubleshooting](#troubleshooting) | [Limits](#limits)

## Install

Python 3.11 or newer.

Each release carries three files: the wheel `contractum-0.1.0-py3-none-any.whl`, the source archive `contractum-0.1.0.tar.gz` and `SHA256SUMS`. Install the wheel straight from its release URL, always naming the version (the URL is the pin):

```bash
WHEEL=https://github.com/rkisliak/contractum/releases/download/contractum-v0.1.0/contractum-0.1.0-py3-none-any.whl
uv tool install "$WHEEL"                  # a global command
pipx install "$WHEEL"                     # the same, with pipx
uvx --from "$WHEEL" contractum --help     # run once, nothing installed
uv add "contractum @ $WHEEL"              # as a library in a project
contractum --version
```

**Check the download.** To verify the files before installing, download the wheel and `SHA256SUMS` from the same release and compare (on macOS use `shasum -a 256 -c`):

```bash
curl -fsSLO "$WHEEL"
curl -fsSLO https://github.com/rkisliak/contractum/releases/download/contractum-v0.1.0/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
uv tool install ./contractum-0.1.0-py3-none-any.whl
```

Where the repository publishes artifact attestations (public repositories, or private ones on GitHub Enterprise Cloud), `gh attestation verify contractum-0.1.0-py3-none-any.whl -R rkisliak/contractum` also proves the file was built by the repository's release workflow.

**Private repository.** Release downloads of a private repository need authentication, so a plain URL install fails. Download with the GitHub CLI (signed in with `gh auth login`, or `GH_TOKEN` in CI), then install the local file:

```bash
gh release download contractum-v0.1.0 --pattern 'contractum-*.whl' --pattern SHA256SUMS --repo rkisliak/contractum
sha256sum -c SHA256SUMS --ignore-missing
uv tool install ./contractum-0.1.0-py3-none-any.whl
```

Features that need a third-party package are extras. A missing extra is a clear error that names the one to install.

| Extra | Adds |
|-------|------|
| `pandas` | testing pandas DataFrames |
| `polars` | testing polars DataFrames |
| `delta` | testing Delta Lake tables |
| `sql` | schema-only checks of SQL DDL files |
| `all` | every extra above |

```bash
uv tool install "contractum[delta] @ $WHEEL"
```

**Docker image.** Each release also pushes a hardened, non-root image of the CLI (`clients/python/Dockerfile`, entrypoint `contractum`, working directory `/work`) to `ghcr.io/rkisliak/contractum-cli`, tagged with the version and `latest`; pin the version. It carries the `delta` and `sql` extras, not pandas or polars, and no credential: pass `CONTRACTUM_URL` and `CONTRACTUM_TOKEN` at run time. For a private repository the package is private too: `docker login ghcr.io` first. Before the first release, build it yourself with `docker build -t contractum:test clients/python`.

```bash
docker run --rm ghcr.io/rkisliak/contractum-cli:0.1.0 --version
docker run --rm -v "$PWD:/work:ro" ghcr.io/rkisliak/contractum-cli:0.1.0 test data/orders.parquet --contract payments.orders --frozen --offline
```

The release workflow (`.github/workflows/sdk-release.yml`) builds and smoke-tests the wheel, the source archive and the image on every pull request. Only an `contractum-v*` tag creates the GitHub Release (with `SHA256SUMS` and attestations) and pushes the image; no tag has been pushed yet.

## Sign in

The CLI and the library sign in with a token only. There is no password, browser or device code sign-in. People store a token once per machine with `contractum login`. Pipelines set `CONTRACTUM_TOKEN` and never log in.

| Who | Token |
|-----|-------|
| Local user | A personal access token (`dcp_<public_id>_<secret>`). Create it in the web UI under the account menu (your initials) > Access tokens. A read-only token is enough. |
| Entra user | The same: a personal access token from the account menu (your initials) > Access tokens. |
| Local service account | Its service-account key (`dcp_<public_id>_<secret>`), from an administrator. |
| Entra service principal | Its own Entra access token for the Contractum API app, in `CONTRACTUM_TOKEN`. Or a local service account key. |

```bash
contractum config set url https://contractum.example.com
printf '%s' "$TOKEN" | contractum login --token -
contractum whoami          # who the token belongs to, never the token itself
contractum logout          # forget the stored token of this profile
```

On a terminal, `contractum login` with no option asks for the token without echo. Without a terminal it stops and tells you to use `--token -` or `CONTRACTUM_TOKEN`.

`login` checks the token with the server (`GET /api/v1/identity/me`) before it stores it. `logout` only forgets the stored token and makes no network call. The token stays valid until it expires or is revoked. Revoke a personal access token in the web UI.

A token is never accepted as a command-line argument (it would land in shell history and process lists): `--token` takes only `-` and reads stdin. Token examples in this guide are placeholders of the form `dcp_<public_id>_<secret>`.

**Entra service principal in GitHub Actions.** Sign in with `azure/login` and workload identity (OIDC, `permissions: id-token: write`), then fetch an access token for the Contractum API app in the same job:

```yaml
permissions:
  id-token: write
  contents: read
steps:
  - uses: azure/login@v2
    with:
      client-id: ${{ vars.AZURE_CLIENT_ID }}
      tenant-id: ${{ vars.AZURE_TENANT_ID }}
      allow-no-subscriptions: true
  - name: Test data against the contract
    env:
      CONTRACTUM_URL: https://contractum.example.com
    run: |
      CONTRACTUM_TOKEN=$(az account get-access-token --scope api://<api-app-id>/.default --query accessToken -o tsv)
      export CONTRACTUM_TOKEN
      contractum test data/orders.parquet --contract payments.orders
```

The token does not refresh. It lasts about 60 to 90 minutes, so fetch it in the job that uses it. An administrator must register the service principal in Contractum first (see the admission section of [Entra setup](../admin/entra-setup.md)).

**Where the token is kept.** In the OS keyring when one is usable, otherwise in a `0600` credentials file in the data directory. `CONTRACTUM_KEYRING=0` forces the file and `CONTRACTUM_KEYRING=1` requires the keyring. The stored token is used only for the server it was issued for.

**Order.** `CONTRACTUM_TOKEN` first, then the token stored by `contractum login`.

## Configure

Connection settings live in named profiles in one TOML file. Credentials never do.

```bash
contractum config set url https://contractum.example.com
contractum config set ca_bundle /etc/ssl/company-ca.pem
contractum --profile prod config set url https://contractum.example.com
contractum config use prod       # make prod the default profile
contractum config list
contractum config get url
contractum config unset ca_bundle
contractum config path           # where the profiles file is
```

Profile keys: `url`, `ca_bundle`, `allow_insecure_http`.

| Setting | Flag | Environment variable |
|---------|------|----------------------|
| Profile | `--profile NAME` | `CONTRACTUM_PROFILE` |
| API base URL | `--url URL` | `CONTRACTUM_URL` |
| CA certificates (PEM) | `--ca-bundle FILE` | `CONTRACTUM_CA_BUNDLE`, or `SSL_CERT_FILE` |
| Plain http (local development only) | `--allow-insecure-http` | `CONTRACTUM_ALLOW_INSECURE_HTTP` |
| Token | `login --token -` | `CONTRACTUM_TOKEN` |

Precedence: **flag over environment variable over profile**. Other variables: `CONTRACTUM_CONFIG` (profiles file), `CONTRACTUM_DATA_DIR` (credentials file), `CONTRACTUM_CACHE_DIR` (contract cache), `CONTRACTUM_FROZEN=1` (pytest plugin, see below), `CONTRACTUM_DEBUG=1` (a traceback on an unexpected error).

Global flags go before the command: `--no-color` and `-q`/`--quiet` (only results and errors).

**Company CA.** A server behind a private CA needs the CA file, once: `contractum config set ca_bundle /etc/ssl/company-ca.pem`, or `--ca-bundle`, or `CONTRACTUM_CA_BUNDLE` in a pipeline.

## Find contracts

```bash
contractum list                          # contracts you can read
contractum list orders --domain payments --status active --tag pii
contractum list --folder acme/payments --all --json
contractum versions payments.orders      # Latest, Preview, older; deprecation and sunset
contractum show payments.orders          # the stored bytes of the latest version
contractum show payments.orders@1.4.0 --format yaml
contractum show payments.orders --metadata
contractum show payments.orders --report # the stored publish report
contractum show payments.orders --version preview -o orders-preview.yaml
```

**Reference grammar.** Wherever a contract is named:

```text
[FOLDER-PATH:]ODCS-ID-OR-UUID[@latest|@preview|@X.Y.Z]
```

`payments.orders` (an ODCS id), `acme/payments:payments.orders` (with a folder path, to tell apart contracts that share an ODCS id), a contract UUID, and the version suffixes `@latest` (the default), `@preview` and an exact SemVer such as `@1.4.0`. An ambiguous reference stops with exit code 2 and lists the candidates: add the folder path.

**Preview** is the upcoming version of a contract that is not published as Latest yet. It can be fetched and tested like any other version (see "Preview and compare").

## Pull and lock

`pull` pins contracts in your repository: it writes the contract files and `contractum.lock`, which records the version and the content hash of every pulled contract. Commit both.

```bash
contractum pull payments.orders@1.4.0 payments.customers@latest
contractum pull                        # resolve the lock's own references again
contractum pull --dir contracts --lock contractum.lock payments.orders
contractum pull --frozen               # only the pinned versions; fail on any drift
contractum pull --offline              # only lock, files and cache; no URL, no network
contractum pull --fail-on-deprecated --sunset-within 30 payments.orders
```

- Default locations: files in `contracts/`, lock in `contractum.lock` (`--dir`, `--lock`).
- `--frozen` never resolves `latest` or `preview`: it reads the versions the lock pins and fails when the server's content hash or the file on disk differs. This is the reproducible CI mode.
- `--offline` needs neither a URL nor a credential. It reads the lock, the pulled files and the cache, and verifies each hash.
- A deprecated version warns. `--fail-on-deprecated` and `--sunset-within DAYS` turn that into exit code 1, after the files and the lock were written.
- `--json` prints a JSON summary.

**Line endings (Windows).** The lock hashes the exact bytes of each pulled file. If Git rewrote line endings on checkout (`core.autocrlf=true`, the Git for Windows default), the files would no longer match the lock and `--frozen` or `--offline` would stop with `does not match the lock`. So `pull` also writes `.gitattributes` lines that turn Git's line-ending conversion off for those files. Commit them with the contracts:

- `contracts/.gitattributes` gets `* -text`, which covers every file in the contracts folder. When the folder is not a subfolder of the lock's folder, or is a repository root, it gets one line per pulled file instead (`/<file> -text`).
- The `.gitattributes` next to the lock gets `/contractum.lock -text` (the lock's own name). A lock name with spaces or other special characters gets a warning instead, and you add the line yourself.

`pull` only adds missing lines: your own lines stay as they are, in their order. It writes `-text` and not `eol=lf`, because `eol=lf` would let Git change a CRLF that the server's own bytes hold. `--frozen` and `--offline` never touch `.gitattributes`.

To opt out, pass `--no-gitattributes`, set `CONTRACTUM_GITATTRIBUTES=0`, or call `pull(..., gitattributes=False)` from Python. Then keep the bytes exact yourself, with the same lines.

**Line endings of other files.** Every text file `contractum` writes (the lock, `.gitattributes`, JSON, JUnit and Markdown reports, `show -o` output in `yaml` or `json`, the profiles file) uses LF on every operating system. Pulled contract files and `show -o` in the `original` format are the server's exact bytes.

**Cache.** Every fetched document is cached, so repeated runs and `--offline` runs need no network. The cache lives in the user cache directory (`contractum`), or in `CONTRACTUM_CACHE_DIR`. It is safe to delete.

## Test data

```bash
contractum test data/orders.parquet --contract payments.orders
contractum test data/orders.csv --contract payments.orders@1.4.0 --object orders
contractum test data/orders.parquet --contract-file contracts/orders.yaml   # no server needed
contractum test data/orders.parquet --contract payments.orders --frozen
contractum test warehouse/orders_delta --contract payments.orders --format delta
contractum test schema/orders.sql --contract payments.orders --dialect snowflake
contractum test data/orders.parquet --contract payments.orders --schema-only
```

**What you can test.** A file (`parquet`, `csv`, `tsv`, `json`, `ndjson`; the suffix decides, or `--format`), a Delta table directory (`delta`, extra `delta`) or a SQL `CREATE TABLE` file (`ddl`, extra `sql`, `--dialect` names the SQL dialect). From Python also pandas, polars and Arrow objects. A DDL file is never executed: it is parsed for column names, types and NOT NULL markers only.

**Which contract.** `--contract REF` (fetches, or reads the lock with `--frozen`/`--offline`), or `--contract-file FILE` for a local ODCS file. `--version VERSION` overrides the version in `--contract`. A contract with several schema objects needs `--object NAME`.

**Options.**

| Option | Meaning |
|--------|---------|
| `--schema-only` | check column names and types only, without reading rows |
| `--extra allow\|warn\|fail` | what to do with columns the contract does not list (default `warn`) |
| `--ignore-case` | match column names without regard to case |
| `--strict-types` | require float or decimal columns for number, and fail (not warn) on values outside a format width |
| `--sample N\|FRACTION` | test N random rows, or a fraction of them; `--seed` makes it repeatable |
| `--limit N` | test only the first N rows |
| `--max-failures N` | failing values shown per check (default 5) |
| `--mask auto\|all\|none` | hide failing values: by classification (default), always, or never |
| `--samples` / `--no-samples` | show or hide failing values |
| `--strict` | exit 1 on warnings too |
| `--fail-on-skipped` | exit 1 when a check was skipped |
| `--fail-on-deprecated`, `--sunset-within DAYS` | fail when the contract version is deprecated or about to sunset |

**What is checked.** Column presence, types and logical types, required and unique columns, enumerations, ranges, patterns, lengths, formats and the ODCS library quality rules (the eight operators, in rows or in percent). Nested properties (objects and array items) are checked through Arrow structs and lists, to ten levels. Common vendor physical types (`VARCHAR(20)`, `DECIMAL(12,2)`, `TIMESTAMP_NTZ`, `NUMBER`, `BIGINT` and similar) are mapped to Arrow types. Quality-rule severities `warning`, `warn` and `info` are warnings; everything else is an error.

**Lenient defaults.** Some checks only warn by default, so a warning never fails the run on its own:

- A quality rule with severity `warning`, `warn` or `info` warns.
- A `number` column also accepts whole-number columns.
- An integer `format` (`i8` to `i128`, `u8` to `u128`) or the number format `f32` is a value range. A value outside it warns. `f64` needs no check, and an unknown format is skipped with a reason.

`--strict-types` makes the last two strict: `number` needs a float or decimal column, and a value outside a format width fails. A gate that must also fail on contract warnings runs `contractum test --strict`.

**Sampling and masking.** A failure shows at most `--max-failures` values, each cut to 80 characters with control characters escaped. A column whose contract classification is `pii`, `restricted`, `confidential`, `sensitive`, `secret` or a level of 3 to 5 shows only the type and length of its failing values under `--mask auto`. `--mask all` hides every value and `--no-samples` shows none.

## Reports and exit codes

A run prints a table: a rich table on a terminal, aligned text in logs. Write machine-readable reports at the same time (any combination):

```bash
contractum test data/orders.parquet --contract payments.orders --json report.json
contractum test data/orders.parquet --contract payments.orders --junit report.xml
contractum test data/orders.parquet --contract payments.orders --markdown summary.md
contractum test data/orders.parquet --contract payments.orders --json - | jq .summary
```

- `--json PATH|-`: the full report (schema version 1, with a committed JSON Schema in the package). With `-` stdout carries only JSON and no table is printed.
- `--junit PATH`: JUnit XML, one test suite per tested object, read by every CI system.
- `--markdown PATH`: a summary sized for a pull-request comment.

Files are written atomically. Only masked, bounded sample values ever reach a report.

| Exit code | Meaning |
|-----------|---------|
| 0 | every check passed (warnings and skipped checks do not fail unless `--strict` or `--fail-on-skipped` is set) |
| 1 | the data does not match the contract: a check failed |
| 2 | a usage or other error: bad flags, no credentials, unreachable server, unknown contract, frozen lock mismatch, a check that could not be evaluated. An unexpected internal error also exits 2 and prints only the type name |

Pipelines can therefore tell a data problem (1) from a broken pipeline (2). Interrupting with Ctrl+C exits 130.

## Preview and compare

The Preview is what will become Latest. Test your data against it before it ships:

```bash
contractum test data/orders.parquet --contract payments.orders --version preview
contractum test data/orders.parquet --contract payments.orders@preview
contractum compare data/orders.parquet --contract payments.orders
contractum compare data/orders.parquet --contract payments.orders --from 1.4.0 --to preview
contractum compare data/orders.parquet --contract payments.orders --json compare.json --markdown compare.md
```

`compare` tests the same data against two versions (default: from `latest` to `preview`) and prints which checks changed, which of them newly fail in the target version, and the server's classified diff of the two versions for context. It takes the dataset and check options of `test`, plus `--json` and `--markdown`. Exit code 1 when the target version has a failed check, 2 on any error, otherwise 0.

## pytest plugin

Installing the package registers a pytest plugin with no cost until you use it. Add the release wheel as a test dependency of the project, pinned by its URL (for a private repository, commit the downloaded wheel or install it in CI after `gh release download`, as in "Install"):

```bash
uv add --dev "contractum @ https://github.com/rkisliak/contractum/releases/download/contractum-v0.1.0/contractum-0.1.0-py3-none-any.whl"
```

```python
from contractum import assert_conforms


def test_orders(contractum_contract):
    assert_conforms(load_orders(), contractum_contract("payments.orders"))
```

- `contractum_contract("REF")` returns the contract version (fetched once per reference per session); `contractum_client` is the session's client.
- `assert_conforms(data, contract, object=None, max_failures=5, mask="auto", strict=False, **options)` returns the report when the data conforms and raises `AssertionError` otherwise, naming the contract version, each failed check, the counts and masked, bounded sample values.
- The contract argument can be a contract version, an ODCS document (mapping, text or file path); references are resolved only by `contractum_contract`.

```bash
pytest --contractum-frozen                       # read the pinned versions from contractum.lock
pytest --contractum-frozen --contractum-offline  # no network at all
pytest --contractum-lock ci/contractum.lock --contractum-frozen
CONTRACTUM_FROZEN=1 pytest
```

`contractum_lock` in `pytest.ini` or `pyproject.toml` sets the lock path for the project. `-p no:contractum` switches the plugin off.

## Library use

```python
from contractum import Client

with Client.from_env() as client:  # CONTRACTUM_* variables and the profile file
    contract = client.get("payments.orders")  # Latest
    preview = client.get("payments.orders@preview")
    report = contract.test(df)  # pandas, polars, Arrow or a path
    assert report.ok, report.summary_text()
    schema = contract.to_arrow_schema()  # the contract as a pyarrow Schema
```

- `Client.from_env()` builds a client from arguments, `CONTRACTUM_*` variables and the profile. `Client(url, token=...)` builds one explicitly.
- `client.get(ref)` fetches a version (cached); `client.list_contracts()`, `client.versions(ref)` and `client.pull(refs)` mirror the CLI.
- `ContractVersion.test(data, **overrides)` returns the same `Report` as the CLI; overrides are `TestOptions` fields such as `strict=True`, `limit=1000`, `schema_only=True`.
- `run_test(contract, data, options=TestOptions(...))` tests against a contract document, file or text without a server.
- `ContractVersion.to_arrow_schema(object_name=None)` is the contract as a pyarrow `Schema`, for creating or checking tables.
- Errors derive from `contractum.ContractumError` (`AuthError`, `NotFoundError`, `NetworkError`, `FrozenLockError`, and so on); each carries a `hint`.

## CI recipes

These are documented recipes, not packaged integrations. Each one uses the same `contractum test` call and its exit code. Set `CONTRACTUM_URL` and `CONTRACTUM_TOKEN` from your CI's secret store; never write them into the file.

**GitHub Actions, with `uvx`:**

```yaml
jobs:
  contract-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: Test data against the contract
        env:
          CONTRACTUM_URL: ${{ vars.CONTRACTUM_URL }}
          CONTRACTUM_TOKEN: ${{ secrets.CONTRACTUM_TOKEN }}
          WHEEL: https://github.com/rkisliak/contractum/releases/download/contractum-v0.1.0/contractum-0.1.0-py3-none-any.whl
        run: uvx --from "$WHEEL" contractum test data/orders.parquet --contract payments.orders --junit report.xml --markdown summary.md
      - name: Show the summary
        if: always()
        run: cat summary.md >> "$GITHUB_STEP_SUMMARY"
```

If the Contractum repository is private, download the wheel first with `gh release download contractum-v0.1.0 --pattern 'contractum-*.whl' --repo rkisliak/contractum` (with `GH_TOKEN` set to a token that can read that repository; the workflow's own `GITHUB_TOKEN` reads only its own repository) and use `uvx --from ./contractum-0.1.0-py3-none-any.whl contractum ...`.

**GitHub Actions, with the image** (reproducible and offline, from the committed lock):

```yaml
      - name: Test data against the pinned contract
        run: docker run --rm -v "$PWD:/work:ro" ghcr.io/rkisliak/contractum-cli:0.1.0 test data/orders.parquet --contract payments.orders --frozen --offline
```

**GitLab CI:**

```yaml
contract-test:
  image: python:3.13-slim
  variables:
    WHEEL: https://github.com/rkisliak/contractum/releases/download/contractum-v0.1.0/contractum-0.1.0-py3-none-any.whl
  script:
    - pip install "$WHEEL"
    - contractum test data/orders.parquet --contract payments.orders --junit report.xml
  artifacts:
    when: always
    reports:
      junit: report.xml
```

**Airflow** (a `BashOperator` is enough):

```python
from airflow.operators.bash import BashOperator

check_orders = BashOperator(
    task_id="check_orders_contract",
    bash_command="contractum test /data/orders.parquet --contract payments.orders --frozen",
)
```

**dbt** (an `on-run-end` step, or the CI step after `dbt run`, tests an export of the model):

```bash
dbt run --select orders
contractum test target/orders.parquet --contract payments.orders --frozen --fail-on-skipped
```

**Dagster** (an asset check that calls the library):

```python
import dagster as dg
from contractum import Client


@dg.asset_check(asset="orders")
def orders_conform_to_contract(orders):
    with Client.from_env() as client:
        report = client.get("payments.orders").test(orders)
    return dg.AssetCheckResult(passed=report.ok, description=report.summary_text())
```

Reproducible CI: run `contractum pull payments.orders@1.4.0` once, commit `contracts/` and `contractum.lock`, and test with `--frozen`. A change of the pinned contract is then a reviewed commit, and `--offline` needs no credential at all.

## Security

- **Read-only.** The client sends only GET (and HEAD) requests, never a POST. Anything else is refused inside the client.
- **Secrets.** Tokens come from the environment or the credential store, never from arguments. They are not printed, logged, or written into reports, errors or the cache. Use a read-only PAT, a service-account key or an Entra service principal token with the narrowest folder access that works.
- **Masking.** Failing values of classified columns are masked by default, and every value is bounded and escaped (see "Test data").
- **Integrity.** `pull` records a SHA-256 of each contract. `--frozen` and `--offline` verify it and stop on any difference, so a tampered file or cache entry is never used.
- **TLS.** An `https` URL is required. A plain `http` URL needs `--allow-insecure-http` and is for local development only.

## Troubleshooting

| Message or symptom | Cause and fix |
|--------------------|---------------|
| exit 2, `no token is configured` | Run `contractum login --token -`, or set `CONTRACTUM_TOKEN` in the pipeline |
| exit 2, the stored credential `cannot be used` | It was written by an earlier pre-release; run `contractum logout`, then `contractum login --token -` |
| exit 2, `principal-not-registered` | The Entra user or group is not registered in Contractum; ask an administrator (see the admission section of [Entra setup](../admin/entra-setup.md)) |
| exit 2, ambiguous reference | Several contracts share the ODCS id; add the folder path: `acme/payments:payments.orders` |
| exit 2, `does not match the lock (content hash differs)` | A pulled file or the cache differs from the lock; run `contractum pull` again and review the diff before committing. On Windows, check that Git does not rewrite the line endings of the pulled files (see "Line endings" under "Pull and lock") |
| exit 2, reference missing from the lock | The reference was never pulled; run `contractum pull REF` and commit the lock |
| exit 2, the server certificate could not be verified | Pass `--ca-bundle`, or set `CONTRACTUM_CA_BUNDLE`, to your CA file |
| exit 2, a stored token belongs to another server | Run `contractum login --token -` for this URL |
| exit 2, an extra is not installed | Install the extra named in the message, for example `contractum[delta]` |
| exit 2 for no apparent reason | `CONTRACTUM_DEBUG=1 contractum ...` prints a traceback |
| exit 1 | The data does not match the contract; read the failed checks in the table or the report |

## Limits

- Quality rules of type `sql` and `custom` are skipped, each with its reason in the report. The client never runs SQL or user code. Use `--fail-on-skipped` to make a skip fail the run.
- A `physicalType` that has no Arrow mapping, nesting deeper than ten levels and unsupported formats are skipped with a reason. A skipped check is never reported as passed.
- Local governance rules (the Policy Manager rule sets) are not evaluated: a published version has already passed every mandatory rule of its folder, and `contractum show --report` displays the stored publish report.
- The client tests data it can read locally (files, Delta tables, DDL, DataFrames). It does not connect to databases or query engines, generate synthetic data, or report results back to Contractum.
- Contracts are ODCS; versions are read through the consumer API, so a draft is never testable.
