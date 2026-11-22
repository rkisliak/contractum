# Upgrade, roll back, back up and restore

This page covers day-two operations for a Helm release named `dc` in the namespace `contractum`.
Change the names if yours differ.

## Back up

Back up these three things together, and always before an upgrade.

### PostgreSQL databases

Back up all four databases: `identity`, `contracts`, `validation` and `audit`. They refer to each
other's ids, so always restore them together, from the same point in time.

- **Managed PostgreSQL:** use your service's point-in-time restore or scheduled backups, or run
  `pg_dump` per database with each service's login.
- **Bundled PostgreSQL (evaluation):** dump each database from the `postgres-0` pod:

  ```bash
  for db in identity contracts validation audit; do
    kubectl -n contractum exec postgres-0 -- pg_dump -U postgres -Fc "$db" > "$db.dump"
  done
  ```

  Store the dump files somewhere safe outside the cluster.

### The NATS JetStream volume

JetStream holds events in transit: audit and access events, and notices waiting for delivery. The
audit database is the durable record, so losing the stream loses at most events that were not
delivered yet. If that matters to you, back up the JetStream storage too (the bundled NATS keeps
it on the PersistentVolumeClaim of the `nats-0` pod; use a volume snapshot).

### The Secrets

Keep every value from the Secrets in your secret manager. Two of them cannot be recreated:

- Without `jwt_signing_key.pem` (in `dc-identity`), tokens that identity issued stop verifying,
  and everyone must sign in again.
- Without `webhook_secret_key` (in `dc-contracts`), the stored webhook secrets cannot be
  decrypted, and every webhook needs a new secret.

The database and NATS passwords must match what the servers hold, so back them up as well. Never
commit Secret values to a repository.

## Upgrade

1. Back up (see above).
2. Note the migration head of each database. In the web app, open **Administration > Settings >
   Platform** and look at **About**. You need this to decide later whether a rollback is safe.
3. Use the chart of the new release: its default image tag is that release. If you set
   `global.imageTag` or run from your own registry, mirror or build the new images first (see
   [Images](deploy-kubernetes.md#images)) and set the new tag.
4. Run:

   ```bash
   helm upgrade dc deploy/helm/contractum -n contractum -f my-values.yaml
   ```

5. Watch the migration Job and the pods:

   ```bash
   helm history dc -n contractum              # the newest revision number
   kubectl -n contractum get jobs,pods
   kubectl -n contractum logs job/migrate-r<revision> --all-containers
   ```

What happens during an upgrade:

- Each upgrade starts a new Job `migrate-r<revision>`. It brings the four databases, one after
  the other, to the schema of the new images. Each step retries while the database is not ready
  yet. The Job is retried up to `migrations.backoffLimit` times (default 6) and is removed one day
  after it finishes (`migrations.ttlSecondsAfterFinished`).
- Every new service pod waits in its `wait-for-migrations` init container until its database is
  at the new schema. No pod serves traffic on an old schema, and two replicas never migrate at
  the same time.
- `api-docs` restarts on every upgrade, so the merged API document matches the new services.
- Settings saved in the web app are kept. The values in your values file stay the defaults below
  them (see [Runtime settings](settings.md)).

To change only configuration (no new images), run the same `helm upgrade` with the changed values.
A migration Job still runs, and finds nothing to do.

The bundled Postgres and NATS run as uid and gid 10001. A data volume owned by another user (for
example one created by the images' own users, Postgres 999 or NATS root) is not usable by them, and
their pods do not become ready. Move such an evaluation release over with a dump:

1. Back up the databases (see [Back up](#back-up)) and stop the services.
2. Delete the two StatefulSets and their volumes:
   `kubectl -n contractum delete statefulset postgres nats`, then
   `kubectl -n contractum delete pvc data-postgres-0 data-nats-0`.
3. Upgrade, then restore the databases (see [Restore](#restore)).

## Roll back

Migrations only move forward. Whether a rollback is safe depends on the release you are leaving:

- **The newer release added no migration** (the migration heads in **About** did not change):
  roll back with Helm. `helm history` lists the revisions.

  ```bash
  helm rollback dc <revision> -n contractum
  ```

- **The newer release added a migration:** the older images do not know the newer schema, so
  `helm rollback` alone is not safe. Restore the database backup you took before the upgrade (see
  [Restore](#restore)), then roll back with Helm.

## Restore

Restore all four databases from the same backup, and run the images of the same version that took
the backup.

1. Stop the services that use the databases:

   ```bash
   kubectl -n contractum scale deployment identity contracts validation audit mail --replicas=0
   ```

2. Restore each database.
   - **Managed PostgreSQL:** use your service's restore, for all four databases to the same point
     in time.
   - **Bundled PostgreSQL:** load each dump file into the `postgres-0` pod:

     ```bash
     for db in identity contracts validation audit; do
       kubectl -n contractum exec -i postgres-0 -- pg_restore -U postgres --clean --if-exists -d "$db" < "$db.dump"
     done
     ```

3. If you also restore the Secrets, recreate them before the services start again.
4. Start the services again. A `helm upgrade` (or `helm rollback`) with your values restores the
   replica counts from your values file and runs a migration Job, which finds the restored schema
   at the version of those images.

## Uninstall

`helm uninstall dc -n contractum` removes the release. It keeps the Secrets and the bundled Postgres
and NATS volumes (PersistentVolumeClaims). Delete those explicitly to remove all data.
