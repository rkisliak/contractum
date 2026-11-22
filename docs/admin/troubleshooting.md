# Troubleshooting

Each entry lists a symptom, what to check and how to fix it. The commands assume the namespace
`contractum`. For problems with the docker compose development stack, see
[the local stack guide](../dev/local-stack.md).

## Where the logs are

Every service writes JSON logs to standard output. Read them with `kubectl logs`:

```bash
kubectl -n contractum get pods
kubectl -n contractum logs deployment/identity          # also contracts, validation, audit, mail, ui, gateway
kubectl -n contractum logs deployment/contracts --since=15m
kubectl -n contractum describe pod <pod name>           # events: scheduling, image pulls, probes
```

Every API answer carries an `X-Request-ID` header. Search the logs for it to follow one request
through the services. Logs never contain passwords, tokens or the `Authorization` and `Cookie`
headers.

## Pods stay Pending

**Check:** `kubectl -n contractum describe pod <pod name>` and read the events at the end.

**Fix:**

- `Insufficient cpu` or `Insufficient memory`: the node cannot fit the requests. Add nodes, or
  lower the requests in your values file (each component has a `resources` block).
- `unbound immediate PersistentVolumeClaims`: the bundled Postgres or NATS needs a volume. Set
  `postgres.storageClass` and `nats.storageClass` to a storage class your cluster has, or use
  managed services.

## Image pull errors

**Symptom:** `ErrImagePull` or `ImagePullBackOff`.

**Check:** `kubectl -n contractum describe pod <pod name>` shows the image name it tried.

**Fix:**

- The name is `[global.imageRegistry/]contractum/<component>:<global.imageTag>`. The defaults
  give `ghcr.io/rkisliak/contractum/<component>:<chart appVersion>`. With your own registry or
  tag, make sure every image was pushed with that tag.
- A private registry needs a pull Secret listed in `global.imagePullSecrets`.
- Images you built and imported into a local cluster need `global.imageRegistry` set to `""` and
  `global.imageTag` to their tag (for example `dev`).
- On a local kind cluster, images you built with Docker are not in the cluster until you import
  them into every node. See
  [Try it on a local cluster (kind)](deploy-kubernetes.md#try-it-on-a-local-cluster-kind).

## The migration Job failed or pods wait in `wait-for-migrations`

Every service pod waits in its `wait-for-migrations` init container until the migration Job has
brought its database to the current schema.

**Check:**

```bash
kubectl -n contractum get jobs
kubectl -n contractum logs job/migrate-r<revision> --all-containers
```

The Job has one init container per database (`identity`, `contracts`, `validation`, `audit`).
`database not ready (attempt N)` lines mean it cannot reach or log in to the database yet.

**Fix:**

- **Cannot connect:** check `postgres.host` and `postgres.port`, and that the database server
  accepts connections from the cluster. With TLS or a special user name format, put a full
  `database_url` into the service Secret (see
  [Managed PostgreSQL](deploy-kubernetes.md#managed-postgresql)).
- **Password authentication failed:** the `db_password` in the service Secret must match the
  database role's password.
- **Extension `ltree` missing:** create it in the `identity` database.
- **A missing Secret:** a pod that mounts a Secret that does not exist stays in
  `ContainerCreating`. Create it (see [Secrets](deploy-kubernetes.md#secrets)).
- After a rollback to older images, a failing migration step usually means the newer release
  added a migration. See [Roll back](upgrade-backup.md#roll-back).

## The gateway is not reachable

**Check:**

```bash
kubectl -n contractum get service gateway
kubectl -n contractum get pods -l app.kubernetes.io/name=gateway
kubectl -n contractum logs deployment/gateway
```

**Fix:**

- `EXTERNAL-IP` stays `<pending>`: the cluster has no load balancer. Use
  `kubectl -n contractum port-forward service/gateway 8443:443` (with your `gateway.service.port`
  instead of `443` if you changed it) and open `https://localhost:8443/`, or set
  `gateway.service.type: ClusterIP` and put your own load balancer or ingress in front.
- The browser warns about the certificate: without your own certificate the gateway uses a
  self-signed certificate for `localhost`. Put your certificate into the `dc-gateway-tls` Secret
  and restart the gateway (`kubectl -n contractum rollout restart deployment/gateway`).
- A 404 problem for every request: the path is wrong. Only `/api/v1/<module>/...`, the API docs
  and the web app are routed. `/internal/`, `/healthz`, `/readyz` and `/.well-known/` are never
  routed on purpose.
- A 502 or 503 problem from the gateway: the target service is down or not ready. Check its pod
  and logs.
- A 429 `too-many-requests`: the gateway's rate limit. Slow down the client.
- A 413 `payload-too-large`: the body is over the gateway's 2 MiB limit, or over the contracts
  service's 1 MiB limit for a contract document.
- With your own ingress instead of the bundled gateway, compare its rules with
  [Gateway and ingress](deploy-kubernetes.md#gateway-and-ingress).

## Sign-in problems

**The bootstrap admin cannot sign in.**

- Read the secret again from the Secret the pods mount:
  `kubectl -n contractum get secret dc-identity -o jsonpath='{.data.bootstrap_admin_secret}' | base64 -d`.
- `bootstrap-unavailable`: identity could not read the secret file. The key is missing from the
  Secret or shorter than 32 characters. Fix the Secret and restart identity
  (`kubectl -n contractum rollout restart deployment/identity`).
- `bootstrap-consumed`: the secret was already used to set a password. Sign in as `admin` with
  that password. If nobody knows it, another Global admin can help; the bootstrap secret cannot be
  used again.
- Too many failed attempts lock the account or the client address for a while (see the lockout
  settings in [Runtime settings](settings.md)). Wait, then try again.

**Every call answers 403 `password-rotation-required`.** The session is the restricted bootstrap
session. Set a new password first (see [First run](first-run.md)).

**403 `sign-in-method-disabled`.** An admin turned that sign-in method off. If nobody can sign in
any more, follow [Locked out of sign-in](settings.md#locked-out-of-sign-in).

**Microsoft sign-in fails.** Check the Entra entry in `auth.issuers`, the `ui.entra` values and
the app registrations against [Microsoft Entra ID setup](entra-setup.md). Make sure the identity
pods may reach `login.microsoftonline.com` and `graph.microsoft.com`. The Entra overview under
**Administration > Settings > Entra** has a connection test and shows the Graph client secret's
expiry.

**Every token is refused after a key change.** The tokens were signed with the old signing key.
Follow [Signing key size and rotation](deploy-kubernetes.md#signing-key-size-and-rotation), which
keeps the old public key published while old tokens expire.

## Publish answers 503 `validation-unavailable`

Contracts never publishes without a verdict from the validation service. When it cannot get one,
publish answers 503 `validation-unavailable` and nothing is published. A draft save during the
same outage still succeeds; it only reports that validation was unavailable.

**Check:**

```bash
kubectl -n contractum get pods -l app.kubernetes.io/name=validation
kubectl -n contractum logs deployment/validation
kubectl -n contractum logs deployment/contracts | grep -E 'validation_(unreachable|bad_status|bad_answer|hash_mismatch)'
```

**Fix:**

- In the validation logs, `rule_worker_not_ready` or out-of-memory kills point at the rule worker.
  Keep the pod memory limit well above `validation.workerMemoryBytes`, and give the pod enough CPU.
- `validation_unreachable` in the contracts logs: validation is down or not ready. Check its pod.
- A busy validation service answers 503 with `Retry-After: 1`. Retry the publish; add replicas if
  it happens often.

## Mail is not delivered

Notices are asynchronous: contracts records a notice, its delivery worker sends it to the mail
service, and the mail service passes it to your SMTP server. The mail service never retries;
contracts retries on its retry schedule.

**Check:**

```bash
kubectl -n contractum logs deployment/mail | grep mail_send_failed
kubectl -n contractum logs deployment/mail --previous      # if the pod restarts
```

**Fix:**

- The mail pod does not start: check `smtp.tls`. `none` is only allowed for an in-cluster or
  loopback host and never together with `smtp.username`.
- `mail_send_failed` with `smtp-not-configured`: `smtp.host` is empty.
- `smtp-error` or an `smtp-<code>` reply: the SMTP server refused the connection or the message.
  Check the host, port, TLS mode, `smtp.username` and the `smtp_password` key in the `dc-mail`
  Secret.
- With `mailpit.enabled: true`, mail is caught by Mailpit and never delivered.
- Use **Send test email** on **Administration > Settings > Notifications** to test the path end
  to end.
- The delivery state of each notice (per channel) is on the subscriber card's **Notices** tab.

## Webhooks are not sent

- Saving a card with a webhook URL answers 422 `webhook-host-not-allowed`: add the host to the
  allowed webhook hosts (see [Runtime settings](settings.md#notifications)). An empty list allows
  no host.
- 503 `settings-unavailable`: contracts could not read the settings from identity. Check that
  identity is running; deliveries wait and are retried.
- A webhook URL must use HTTPS and resolve to a public address.

## An audit event is missing

- Check that the `nats` pod (or your NATS server) is healthy, and look for
  `outbox_relay_publish_failed` in the logs of the service that made the change. Events wait in
  that service's database until NATS accepts them.
- Access events (who read a contract) are best effort. During a NATS outage some can be lost by
  design, which shows as `access_events_dropped` in the logs.
- The audit query answers 503 when identity cannot answer the permission checks. It fails closed
  instead of showing unfiltered results.

## The API docs answer 503

`/api/v1/openapi.json` answers 503 when `api-docs` could not merge the service documents at start.
Check `kubectl -n contractum logs deployment/api-docs`, then restart it once all services are
ready: `kubectl -n contractum rollout restart deployment/api-docs`.
