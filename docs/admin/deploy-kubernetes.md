# Deploy Contractum on Kubernetes

Contractum is deployed with the Helm chart in `deploy/helm/contractum`. It runs on any Kubernetes
cluster: AKS, EKS, GKE, OpenShift-compatible or on-premises.

- To try it on your own machine first, see [Try it on a local cluster (kind)](#try-it-on-a-local-cluster-kind).
- To run the docker compose stack for development and tests, see
  [the local stack guide](../dev/local-stack.md). Compose is not a production deployment.

After the install, continue with [First run](first-run.md). For later releases, see
[Upgrade, roll back, back up and restore](upgrade-backup.md).

## What gets deployed

| Component | Kind | Notes |
|-----------|------|-------|
| `identity`, `contracts`, `validation`, `audit` | Deployment + Service | Python services, each with its own database. Port 8000. |
| `mail` | Deployment + Service | Sends notification email through your SMTP server. Port 8000. |
| `ui` | Deployment + Service | The web app (static files on unprivileged nginx). Port 8080. |
| `api-docs` | Deployment + Service | The merged OpenAPI document and Swagger UI at `/api/v1/docs/`. Port 8080. |
| `gateway` | Deployment + Service | Traefik with the routes, limits and security headers. Optional (see [Gateway and ingress](#gateway-and-ingress)). |
| `problem-responder` | Deployment + Service | The error pages the gateway answers with. Runs with the gateway. |
| `migrate-r<revision>` | Job | Runs the database migrations once per install or upgrade. |
| `postgres`, `nats` | StatefulSet | Optional bundled backing services, for evaluation only. Use managed ones in production. |
| `mailpit` | Deployment | Optional mail catcher, for evaluation only. It never delivers mail. |
| NetworkPolicies | | Only the gateway is reachable from outside the namespace. |

The services find each other by fixed names (`identity`, `contracts`, `postgres`, `nats` and so
on). Install **one release per namespace**.

## Prerequisites

- Kubernetes 1.28 or newer, `kubectl` and Helm 3.
- A container registry the cluster can pull from.
- **PostgreSQL 18** with the `ltree` extension, or the bundled one for evaluation.
- **NATS 2.15 with JetStream** and persistent storage, or the bundled one.
- An SMTP server for notification email.
- A TLS certificate for the public host name (for example from cert-manager).
- Optional: a Microsoft Entra tenant for single sign-on (see [Microsoft Entra sign-in](#microsoft-entra-sign-in)).
- A CNI that enforces NetworkPolicies (most do). Without one the policies are ignored. In that
  case set `global.trustedProxies` to your ingress address range instead of `*` (see
  [Security notes](#security-notes)).

## Try it on a local cluster (kind)

This is for evaluation only, on Docker Desktop Kubernetes, kind or minikube.

1. Build the images locally. From the repository root, run `docker compose build`. It tags them
   `contractum/<component>:dev`, which is the chart's default tag.
2. Make the images visible to the cluster:
   - Docker Desktop with the kubeadm provisioner: nothing to do.
   - kind (including Docker Desktop's kind provisioner): import each image into every node:

     ```bash
     for img in identity contracts validation audit mail ui api-docs; do
       for node in $(kind get nodes 2>/dev/null || docker ps --format '{{.Names}}' --filter name=desktop-); do
         docker save contractum/$img:dev | docker exec -i "$node" ctr -n k8s.io images import -
       done
     done
     ```

3. Create the Secrets. From `deploy/helm/contractum`, run:

   ```bash
   NAMESPACE=contractum bash scripts/create-secrets.sh
   ```

4. Install the release, still from `deploy/helm/contractum`:

   ```bash
   helm install dc . -n contractum --set mailpit.enabled=true --set gateway.service.port=8443
   ```

5. Open `https://localhost:8443/`. Docker Desktop publishes LoadBalancer services on localhost.
   Elsewhere, run `kubectl -n contractum port-forward service/gateway 8443:8443`.
6. Sign in as `admin` with the bootstrap secret. [First run](first-run.md) shows how to read it.

Without your own certificate, the gateway uses a self-signed certificate for `localhost`, so the
browser warns once.

## Build and push the images

From the repository root, with `REG` your registry and `TAG` your release tag:

```bash
REG=myregistry.azurecr.io TAG=1.0.0
for svc in identity contracts validation audit mail; do
  docker build -f infra/docker/service.Dockerfile --build-arg PACKAGE=dc-$svc \
    --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t $REG/contractum/$svc:$TAG .
done
docker build -f infra/docker/api-docs.Dockerfile -t $REG/contractum/api-docs:$TAG .
docker build -f infra/docker/ui.Dockerfile --target runtime -t $REG/contractum/ui:$TAG ui
for img in identity contracts validation audit mail api-docs ui; do docker push $REG/contractum/$img:$TAG; done
```

Set `global.imageRegistry` and `global.imageTag` to match. If the registry needs credentials,
create a pull Secret and list it in `global.imagePullSecrets`.

## Database and event bus

### Managed PostgreSQL

Set `postgres.bundled: false`. Create one database and one login role per service, and the
`ltree` extension in `identity`. Use the passwords you put into the Secrets (see
[Secrets](#secrets)):

```sql
CREATE ROLE identity   LOGIN PASSWORD '<identity db_password>';   CREATE DATABASE identity   OWNER identity;
CREATE ROLE contracts  LOGIN PASSWORD '<contracts db_password>';  CREATE DATABASE contracts  OWNER contracts;
CREATE ROLE validation LOGIN PASSWORD '<validation db_password>'; CREATE DATABASE validation OWNER validation;
CREATE ROLE audit      LOGIN PASSWORD '<audit db_password>';      CREATE DATABASE audit      OWNER audit;
REVOKE ALL ON DATABASE identity, contracts, validation, audit FROM PUBLIC;
\c identity
CREATE EXTENSION IF NOT EXISTS ltree;
```

Set `postgres.host` and `postgres.port`. Some servers need TLS options or a different user name
format (some managed services use `user@server`). Then put a full SQLAlchemy URL into the service
Secret as the key `database_url`, for example
`postgresql+psycopg://identity:<password>@db.example.com:5432/identity?sslmode=require`. It
overrides the host, port, database name, user and password settings.

### External NATS

Set `nats.bundled: false` and `nats.url`. Run NATS 2.15 or newer with JetStream and persistent
storage. Create the per-service users and permissions from `infra/nats/nats.conf.tmpl`: one user
per service, each limited to its own subjects. The comments in the template explain each
permission. The audit service creates the `AUDIT` and `ACCESS` streams itself.

## Secrets

The chart never creates Secrets. Create them before you install. Each service Secret is mounted
read-only at `/run/secrets`, and each key is a file name the service reads.

| Secret (values key) | Keys |
|---------------------|------|
| `dc-identity` (`secrets.identity`) | `db_password`, `nats_password`, `jwt_signing_key.pem` (RSA private key, PEM), `jwt_previous_public_key.pem` (the previous public key during a key rotation), `bootstrap_admin_secret` (one-time admin secret, at least 32 characters), `notifier_credential` (`<public id>:<sha256 hex of the notifier secret>`), `graph_client_secret` (used only with Entra) |
| `dc-contracts` (`secrets.contracts`) | `db_password`, `nats_password`, `webhook_secret_key` (Fernet key: urlsafe base64 of 32 random bytes), `mail_api_key` (`dcp_<public id>_<notifier secret>`, matching `notifier_credential`) |
| `dc-validation` (`secrets.validation`) | `db_password`, `nats_password` |
| `dc-audit` (`secrets.audit`) | `db_password`, `nats_password` |
| `dc-mail` (`secrets.mail`, optional) | `smtp_password` |
| `dc-gateway-tls` (`secrets.gatewayTls`) | type `kubernetes.io/tls`: `tls.crt`, `tls.key` |
| `dc-postgres` (`secrets.postgres`, bundled only) | `postgres_password`, and `<service>_db_password` for the four services |
| `dc-nats` (`secrets.nats`, bundled only) | `nats.conf` (rendered from `infra/nats/nats.conf.tmpl` with the four NATS passwords) |

Any service Secret may also hold `database_url` (see [Managed PostgreSQL](#managed-postgresql)).

### Option A: the script

`deploy/helm/contractum/scripts/create-secrets.sh` generates every value with the same generator
the compose stack uses, and creates the Secrets. It needs Docker and the identity image. It never
overwrites a Secret that already exists. It always uses the default Secret names above.

```bash
NAMESPACE=contractum KUBE_CONTEXT=my-cluster \
TLS_CERT=fullchain.pem TLS_KEY=privkey.pem SMTP_PASSWORD='...' \
IMAGE=myregistry.azurecr.io/contractum/identity:1.0.0 \
  bash deploy/helm/contractum/scripts/create-secrets.sh
```

- `IMAGE` is the identity image that runs the generator (default `contractum/identity:dev`).
- `SKIP_BUNDLED=1` skips the bundled Postgres and NATS Secrets. Use it with an external Postgres
  and NATS. Then copy the four database passwords from the created Secrets into your database
  roles, and the NATS passwords into your NATS users.
- Without `TLS_CERT` and `TLS_KEY`, the gateway gets a self-signed certificate for `localhost`
  only.
- `SMTP_PASSWORD` creates the optional `dc-mail` Secret.

### Option B: a secret manager

Create the same Secrets and keys from Azure Key Vault, AWS Secrets Manager or Vault, for example
with External Secrets Operator or the Secrets Store CSI driver (synced to Kubernetes Secrets). To
rotate a database or NATS password, change it in the server and in the Secret, then restart the
service.

### Signing key size and rotation

New installs get a 3072-bit RSA signing key for the local identity provider. An existing release
keeps the key in its `dc-identity` Secret, because `create-secrets.sh` never changes an existing
Secret. A 2048-bit key still works, and identity logs `signing_key_below_recommended_size` at
start.

To rotate the key by hand and keep tokens of the old key valid until they expire:

1. Generate a key:
   `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out jwt_signing_key.pem`.
2. Save the current public key from the Secret as `jwt_previous_public_key.pem`:

   ```bash
   kubectl -n contractum get secret dc-identity -o jsonpath='{.data.jwt_signing_key\.pem}' \
     | base64 -d | openssl pkey -pubout -out jwt_previous_public_key.pem
   ```

3. Put both files into the Secret and keep every other key. Export the other keys with
   `kubectl get secret dc-identity -o jsonpath`, then recreate the Secret from all files:
   `kubectl -n contractum create secret generic dc-identity --from-file=... --dry-run=client -o yaml | kubectl apply -f -`.
4. Run `helm upgrade` with your values plus
   `--set identity.extraEnv.DC_JWT_PREVIOUS_PUBLIC_KEY_FILE=/run/secrets/jwt_previous_public_key.pem`.
   Identity restarts and serves both keys in its JWKS.
5. Access tokens signed by the old key keep working until they expire (15 minutes). Refresh tokens
   are opaque and are not affected. Afterwards you can remove the previous key from the Secret and
   the variable from your values.

### TLS certificate from Let's Encrypt (cert-manager)

The gateway reads its certificate from the Secret `dc-gateway-tls`. cert-manager can fill that
Secret with a free Let's Encrypt certificate and renew it. This needs a public DNS name, for
example `contractum.example.com`. Let's Encrypt cannot issue a certificate for `localhost` or for
a private name it cannot verify.

**Use DNS validation (DNS-01).** The bundled gateway listens only on 443, and Let's Encrypt's
HTTP-01 check needs port 80 reachable from the internet. DNS-01 proves ownership through a TXT
record instead. It works for private clusters too, and supports wildcard names.

1. Install cert-manager (pin the current release):

   ```bash
   helm repo add jetstack https://charts.jetstack.io && helm repo update
   helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true
   ```

2. Create an issuer for your DNS provider. Cloudflare example: an API token with "Zone DNS edit"
   permission, stored as a Secret in the `cert-manager` namespace:

   ```bash
   kubectl -n cert-manager create secret generic cloudflare-api-token --from-literal=api-token='<token>'
   ```

   ```yaml
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata:
     name: letsencrypt
   spec:
     acme:
       server: https://acme-v02.api.letsencrypt.org/directory
       # Try https://acme-staging-v02.api.letsencrypt.org/directory first: production has rate limits.
       email: platform-team@example.com
       privateKeySecretRef:
         name: letsencrypt-account-key
       solvers:
         - dns01:
             cloudflare:
               apiTokenSecretRef:
                 name: cloudflare-api-token
                 key: api-token
   ```

   Other providers (Azure DNS with workload identity, Route 53, Google Cloud DNS and more) only
   change the `dns01` block. See the cert-manager ACME DNS-01 documentation.

3. Request the certificate into the gateway's Secret. Create it before you install the chart. A
   Secret that already exists is kept by `create-secrets.sh`, so the script then skips the
   gateway certificate.

   ```yaml
   apiVersion: cert-manager.io/v1
   kind: Certificate
   metadata:
     name: dc-gateway-tls
     namespace: contractum
   spec:
     secretName: dc-gateway-tls
     dnsNames: [contractum.example.com]
     issuerRef:
       kind: ClusterIssuer
       name: letsencrypt
   ```

   `kubectl -n contractum get certificate dc-gateway-tls` shows `READY True` once it is issued
   (usually within a few minutes).

4. Point the DNS name at the gateway: an A or CNAME record for the address shown by
   `kubectl -n contractum get service gateway`.

**Renewal.**

- cert-manager renews the certificate about 30 days before it expires and updates the Secret.
- Traefik reads the certificate files at start. Restart the gateway to load the renewed
  certificate: `kubectl -n contractum rollout restart deployment/gateway`.
- To make this automatic, run [Stakater Reloader](https://github.com/stakater/Reloader) and
  annotate the gateway:
  `kubectl -n contractum annotate deployment gateway secret.reloader.stakater.com/reload=dc-gateway-tls`.
  The gateway then restarts whenever the Secret changes.

**With your own ingress** (`gateway.enabled: false`) on ports 80 and 443, HTTP-01 works too. Let
cert-manager manage the ingress certificate in the usual way (the `cert-manager.io/cluster-issuer`
annotation on the Ingress).

**On a local cluster** there is no public name. Use a locally trusted certificate instead:

- create one with [mkcert](https://github.com/FiloSottile/mkcert) (`mkcert localhost 127.0.0.1`)
  and pass it as `TLS_CERT` and `TLS_KEY` to `create-secrets.sh`;
- or use cert-manager's `selfSigned` and `ca` issuers and trust that CA once on your machine.

## Configure the release

Start from `deploy/helm/contractum/values.yaml`. Every option is commented there. A typical
production file:

```yaml
global:
  imageRegistry: myregistry.azurecr.io
  imageTag: "1.0.0"
  publicUrl: https://contractum.example.com
postgres:
  bundled: false
  host: my-postgres.postgres.database.azure.com
nats:
  bundled: false
  url: nats://nats.messaging.svc:4222
smtp:
  host: smtp.example.com
  port: 587
  tls: starttls
  username: contractum
  from: contractum@example.com
gateway:
  service:
    type: LoadBalancer        # or ClusterIP behind your own load balancer
identity:
  replicas: 2
contracts:
  replicas: 2
validation:
  replicas: 2
```

**Mail TLS.** `smtp.tls` is `starttls` by default. `implicit` is the other checked mode. Both
verify the server certificate and host name, and need TLS 1.2 or newer. `none` (plain text) is
only for an in-cluster or loopback host such as the bundled Mailpit. The mail service refuses to
start when `none` names any other host, or when it is combined with `smtp.username`.

**Extra environment.** `identity.extraEnv`, `contracts.extraEnv`, `validation.extraEnv` and
`audit.extraEnv` add environment variables to that service, for example
`--set identity.extraEnv.DC_MAX_SESSIONS_PER_USER=10`.

**Resources.** Every container has CPU and memory requests and limits in `values.yaml`. This
covers the services, the UI, the API docs, the gateway, the problem responder, Postgres, NATS,
Mailpit, the migration Job (`migrations.resources`) and the wait-for-migrations init container of
each service (`waitForMigrations.resources`). The requests fit a single-node evaluation cluster.
Raise the limits per service under load, for example `--set identity.resources.limits.cpu=2`.

**Runtime settings.** Some settings can be changed later in the web app, without a redeploy. The
values above are then the defaults. See [Runtime settings](settings.md).

To install, run `helm install` with your values file. See [First run](first-run.md) for the
install command and what to check, and [Upgrade, roll back, back up and restore](upgrade-backup.md)
for later releases.

## Gateway and ingress

**Bundled Traefik (default).** The chart runs Traefik with the same configuration as the compose
stack (`gateway/traefik.yml` and `gateway/dynamic/`, copied into the chart's `files/` folder).
Expose it with `gateway.service.type: LoadBalancer`, or keep `ClusterIP` and put your own load
balancer in front of the service port (`gateway.service.port`, default 443). Cloud load balancer
settings go into `gateway.service.annotations`.

**Your own ingress (`gateway.enabled: false`).** NGINX Ingress, Traefik IngressRoutes or Azure API
Management work. No authentication lives in the gateway, so security does not depend on it.
Reproduce what the bundled gateway does:

- TLS termination and HSTS.
- Routing:
  - `/api/v1/identity/`, `/api/v1/contracts/`, `/api/v1/validation/` and `/api/v1/audit/` go to
    the service of the same name on port 8000;
  - `/api/v1/openapi.json` and `/api/v1/docs` go to `api-docs:8080`;
  - other `GET` and `HEAD` requests go to `ui:8080`.
- **Never** route `/internal/`, `/healthz`, `/readyz` or `/.well-known/`. Answer them, and unknown
  paths, with 404.
- A request body limit of 2 MiB (the contracts service also caps a contract document at 1 MiB
  itself). Rate limits as in `gateway/dynamic/middlewares.yml`.
- The CORS policy and security headers from `gateway/dynamic/middlewares.yml`.
- Forward `X-Forwarded-For` and `X-Forwarded-Proto`.

## Microsoft Entra sign-in

Entra is off by default. Without it, users sign in with local accounts only, and the Entra
screens cannot reach a directory. The chart has no stand-in directory.

To turn it on, register the applications in your tenant, add an Entra entry to `auth.issuers`,
set `auth.graph.clientId` and the `ui.entra` values, add the key `graph_client_secret` to the
`dc-identity` Secret, and allow egress from the identity pods to `login.microsoftonline.com` and
`graph.microsoft.com`. [Microsoft Entra ID setup](entra-setup.md) walks through every step.

## Security notes

- NetworkPolicies (`networkPolicy.enabled`, default on): clients reach only the gateway. The
  gateway reaches the UI, the API docs and the services. Only the services reach Postgres and
  NATS. Egress is not restricted. Add egress policies if your platform requires them.
- `global.trustedProxies` (default `*`) is the address range whose `X-Forwarded-For` the services
  trust. It drives identity's per-IP login backoff. `*` is safe only while the NetworkPolicies
  keep every path to the services through the gateway. Otherwise set your ingress address range.
- Every container runs on a read-only root file system with no privilege escalation, all
  capabilities dropped and no Kubernetes API token. Every resource names the release namespace,
  so install into a dedicated namespace, not `default`.
- Every pod runs as a non-root user: the services, the UI, the API docs and Traefik above uid
  10000 (Traefik listens on 8443 and the `gateway` Service maps port 443 to it), the bundled
  Postgres as the image's own `postgres` user, 999, which owns its data directory. The one
  exception is the bundled NATS, which runs as the image's root user.
- The bootstrap admin secret works once. It is used up when the admin sets a new password. Keep
  the `dc-identity` Secret restricted anyway: it also holds the signing key.
- The security standard the platform follows is in [ASVS Level 2](security/asvs-l2.md).

## Scaling and sizing

- `identity`, `contracts`, `validation`, `audit` and `mail` can run several replicas.
  - Background jobs (the event relay, notification delivery, the sunset scheduler, audit
    retention) coordinate through database locks.
  - Login backoff is stored in the database.
  - Authorization decisions are never cached.
- `identity` answers every permission check. In a load test, one replica handled about 45 checks
  per second. Add replicas before you raise traffic.
- `validation` runs each rule check in a subprocess capped at `validation.workerMemoryBytes`
  (1 GiB). Keep the pod memory limit well above it (default 3 GiB). Rule timeouts count CPU time,
  so a tight CPU limit slows rule checks without failing them.
- `api-docs` merges the service documents at start. The chart restarts it on every upgrade.
- The bundled Postgres and NATS are single replicas for evaluation, not for production.

## Operations

- **Health:** every service answers `/healthz`, which the readiness and liveness probes use.
  `kubectl -n contractum get pods` shows the state.
- **Logs:** JSON on standard output, for example `kubectl -n contractum logs deployment/identity`.
  Traefik access logs drop the `Authorization` and `Cookie` headers.
- **API documentation:** `https://<public host>/api/v1/docs/`.
- **Problems:** see [Troubleshooting](troubleshooting.md).
- **Uninstall:** `helm uninstall dc -n contractum` keeps the bundled Postgres and NATS volumes
  (PersistentVolumeClaims) and the Secrets. Delete them explicitly to remove all data.

## Verification

The chart was checked on a two-node kind cluster (Kubernetes 1.36) with the bundled Postgres, NATS
and Mailpit:

- the migration Job completed and every pod became Ready;
- the app answered through the gateway's LoadBalancer;
- the browser test suite passed against that deployment: local sign-in and session handling,
  security headers, accessibility, password change, and the role journeys (Policy Manager, an
  editor whose publish is blocked and then fixed, a consumer download, an Org admin reading audit
  and access).

The test suites in this repository start the stack with docker compose. To run the browser suite
against a cluster, you also need the compose-only fake Microsoft Graph (`infra/fake-graph`),
deployed next to the release, with identity pointed at it through `auth.graph` and the compose
Entra issuer entry in `auth.issuers`.
