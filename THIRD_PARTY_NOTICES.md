# Third-party notices

This repository includes or bundles the following third-party components. They remain under
their own licenses, not under the [LICENSE](LICENSE) of this project.

| Component | Where | License |
|---|---|---|
| ODCS JSON Schemas v3.1.0 and v3.2.0 (Bitol, [open-data-contract-standard](https://github.com/bitol-io/open-data-contract-standard)), vendored unmodified at commit `04d1bfd2` | `shared/dc-odcs/src/dc_odcs/schemas/` | Apache License 2.0, full text in [`LICENSE-ODCS-Apache-2.0.txt`](shared/dc-odcs/src/dc_odcs/schemas/LICENSE-ODCS-Apache-2.0.txt) |
| Inter variable font (`@fontsource-variable/inter`) | bundled into the web UI | SIL Open Font License 1.1 |
| Saira font, weight 700 (`@fontsource/saira`), for the Contractum wordmark | bundled into the web UI | SIL Open Font License 1.1 |
| Monaco Editor (`monaco-editor`) | bundled into the web UI | MIT |

Every other dependency is installed from PyPI or npm at build time, under its own license, at the
exact versions pinned in [`uv.lock`](uv.lock) and [`ui/pnpm-lock.yaml`](ui/pnpm-lock.yaml).
