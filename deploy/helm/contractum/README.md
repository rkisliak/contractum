# contractum Helm chart

Installs the Contractum platform on Kubernetes: the identity, contracts, validation, audit and
mail services, the web UI, the API docs, the Traefik gateway with its problem responder, a
migration Job per install or upgrade, NetworkPolicies, and optionally a bundled PostgreSQL, NATS
and Mailpit for evaluation.

Full guide: [docs/admin/deploy-kubernetes.md](../../../docs/admin/deploy-kubernetes.md)
(prerequisites, Secrets, configuration, ingress, Entra, security, scaling, and a local kind trial).

## Install

Run these from `deploy/helm/contractum`:

```bash
bash scripts/create-secrets.sh                    # or create the Secrets with your secret manager
helm install dc . -n contractum -f my-values.yaml
```

The install notes print how to open the gateway and read the one-time admin secret.

## What is in this folder

- `values.yaml`: every option, commented.
- `scripts/create-secrets.sh`: creates every Secret the chart expects, with fresh random values.
  It never overwrites a Secret that already exists.
- `files/`: copies of the compose gateway configuration, the problem responder and the Postgres
  init script. `tests/repo/test_helm_chart_sync.py` fails when a copy differs from its source.
- `templates/`: the Kubernetes resources.
