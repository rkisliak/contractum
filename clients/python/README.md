# contractum

`contractum` is the read-only Python SDK and CLI for published Contractum data contracts. It fetches
the Latest, an older or the upcoming Preview version of a contract through the Contractum API, pins it
in a lock file, and tests your own data against it locally: from the command line, from Python, from
pytest or from CI. It never writes to the server.

It needs Python 3.11 or newer.

## Install

`contractum` ships with every platform release on the GitHub Releases page of the Contractum
repository (tag `vX.Y.Z`, or `vX.Y.Z-rc.N` for a release candidate), not on a package index. Each
release carries the wheel, the source archive and `SHA256SUMS`, with the same version as the server
(`1.1.0rc1` in PEP 440 form for `v1.1.0-rc.1`). Install the wheel from its release URL, for example
with `uv tool install` or `pip install`, or download it with
`gh release download vX.Y.Z --pattern '*.whl' --repo rkisliak/contractum`. To try a change that is
not in a release, install from a checkout with `uv tool install ./clients/python` (it reports the
placeholder version `0.0.0.dev0`).

## Quickstart

```bash
contractum config set url https://contractum.example.com
printf '%s' "$TOKEN" | contractum login --token -     # a personal access token
contractum pull payments.orders@1.4.0                 # writes contracts/ and contractum.lock
contractum test data/orders.parquet --contract payments.orders --frozen --junit report.xml
```

Create the token in the web UI under the account menu (your initials) > Access tokens. Pipelines set `CONTRACTUM_URL` and
`CONTRACTUM_TOKEN` instead of signing in. The exit code is 0 when the data matches the contract, 1 when
it does not and 2 on any other error.

```python
from contractum import Client, assert_conforms

with Client.from_env() as client:
    contract = client.get("payments.orders")
    assert_conforms(load_orders(), contract)
```

## License

`contractum` (this directory) is licensed under the Apache License 2.0: see `LICENSE` and `NOTICE`.
The Contractum server in the rest of the repository keeps its own licence.

## Documentation

Full guide: `docs/user/sdk-cli.md` in the repository. It covers sign-in, configuration, every
command, reports and exit codes, the pytest plugin, CI recipes, troubleshooting and limits.
