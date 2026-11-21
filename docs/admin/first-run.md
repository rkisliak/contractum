# First run

This page takes you from a fresh Helm install to a working platform: the first sign-in, the first
folders and grants, and access for pipelines. It assumes you created the Secrets and a values file
as described in [Deploy Contractum on Kubernetes](deploy-kubernetes.md).

## Install and wait for the pods

1. Install the release:

   ```bash
   helm install dc deploy/helm/contractum -n contractum -f my-values.yaml
   ```

2. Wait for the migrations and the pods:

   ```bash
   kubectl -n contractum get jobs,pods
   ```

   Each install or upgrade starts a Job named `migrate-r<revision>` (`migrate-r1` for the first
   install). It brings the four databases to the current schema. Every service pod waits in its
   `wait-for-migrations` init container until its database is ready, so the services start only
   after the Job completes. To wait in a script:

   ```bash
   kubectl -n contractum wait --for=condition=complete job/migrate-r1 --timeout=10m
   kubectl -n contractum wait --for=condition=Ready pod --all --timeout=10m
   ```

If a pod or the Job does not get there, see [Troubleshooting](troubleshooting.md).

## Open the web app

With the bundled gateway, the install notes print how to reach it:

- With a `LoadBalancer` service (the default), find the address with
  `kubectl -n contractum get service gateway` and open `https://<address>/`, or your public host
  name once DNS points at it.
- Without a load balancer, forward a local port and open `https://localhost:8443/`:

  ```bash
  kubectl -n contractum port-forward service/gateway 8443:443
  ```

  Use the service port you configured in `gateway.service.port` instead of `443` if you changed
  it.

The API documentation is at `https://<host>/api/v1/docs/`.

## Sign in as the bootstrap admin

On its first start, identity creates the ROOT folder and a Global admin named `admin`. The
one-time password for that account is the bootstrap secret: the key `bootstrap_admin_secret` in
the identity Secret (`dc-identity`, unless you changed `secrets.identity`). Read it with:

```bash
kubectl -n contractum get secret dc-identity -o jsonpath='{.data.bootstrap_admin_secret}' | base64 -d
```

1. Sign in as `admin` with that secret.
2. The app asks you to choose a new password right away. The password needs at least 12
   characters, and very common passwords are refused.
3. The app signs you in with the new password.

What to know about the bootstrap sign-in:

- It gives a **restricted session**. It lasts 10 minutes and cannot be refreshed. The only thing
  it can do is change the password. Every other call answers 403 `password-rotation-required`.
- Changing the password **uses up** the bootstrap secret. Signing in with it again answers 409
  `bootstrap-consumed`. Keep the new admin password in your password manager.
- Changing a password ends every other session of that user.

Through the API, the same steps are `POST /api/v1/identity/auth/login` with `username` and
`password`, then `POST /api/v1/identity/auth/password` with `new_password`, using the restricted
access token.

## Create the first folders

Contracts, rule sets and access are organized in a folder tree: ROOT, then Organization, then
Project, then Team. The level of a new folder follows from its parent.

- In the web app, open **Folders**, pick the parent (ROOT is shown as **Root**) and use
  **Create organization**, **Create project** or **Create team**.
- Through the API, call `POST /api/v1/identity/folders` with `parent_id`, `slug` and
  `display_name`.

A folder's slug never changes. A folder can be archived, and an empty folder can be deleted by a
Global admin.

## Create users and groups

- **Local users:** **Administration > Users**, or `POST /api/v1/identity/users` with `username`,
  `password` and `display_name` (and optionally `email`). Only a Global admin creates users. A new
  user has no access until you grant it.
- **Local groups:** **Administration > Groups**, or `POST /api/v1/identity/groups`. Grant roles to
  groups rather than to each person.
- **Entra users, groups and service principals:** after you connect Entra (see
  [Microsoft Entra ID setup](entra-setup.md)), register them with the **New** menu on
  **Administration > Users**, **Groups** or **Service accounts**.

## Grant access

Roles are granted on a folder and are inherited by every folder below it. Each role can be granted
only at the folder kind shown. "Full contract access" means: read contracts and drafts, create and
edit them, publish, promote, deprecate, unpublish and republish versions, and delete contracts.

| Role | Granted at | What it allows |
|------|------------|----------------|
| `global_admin` | Root | Everything, everywhere: users, settings, every folder (including archive and delete), contract, template, rule set and subscriber card, and the audit log. May grant every role. |
| `global_reader` | Root | Read every folder, contract and draft, published templates and rule sets, subscriber cards and the audit log. Changes nothing and grants nothing. |
| `org_admin` | Organization | Full contract access and restoring deleted contracts. Create and rename folders and block inheritance below the Organization. Manage grants, local groups, service accounts and Entra registrations. Create and manage subscriber cards. Read the drift report and the audit log. May grant `org_reader`, every Project and Team role, `policy_manager` and `consumer`. |
| `org_reader` | Organization | Read folders, contracts and drafts, published rule sets and subscriber cards. Grants nothing. |
| `project_admin` | Project | Full contract access. Create and rename folders and block inheritance on its teams. Manage grants. Create and manage subscriber cards. Read the audit log. May grant `project_editor`, every Team role, `policy_manager` and `consumer`. |
| `project_editor` | Project | Full contract access. Read published rule sets and subscriber cards. Grants nothing. |
| `team_admin` | Team | Full contract access. Rename the team. Manage grants. Create and manage subscriber cards. Read the audit log. May grant `team_editor`, `team_reader`, `policy_manager` and `consumer`. |
| `team_editor` | Team | Full contract access. Read published rule sets and subscriber cards. Grants nothing. |
| `team_reader` | Team | Read folders, contracts and drafts, published rule sets and subscriber cards. Grants nothing. |
| `policy_manager` | any folder | Write, publish, deprecate and make mandatory rule sets. Write, publish and deprecate templates. Run dry runs and read the drift report. Read contract metadata and validate contracts, but never read contract content. Read folders and subscriber cards. Grants nothing. |
| `consumer` | any folder | Read folders, published contract versions and published rule sets. Create subscriber cards. Grants nothing. |

**What a consumer can and cannot do.** A consumer can read the folders below its grant, see the
contracts that have a Latest version, fetch every published version, and create subscriber cards
(and manage the cards it created). A consumer cannot see drafts, unpublished versions or a
contract's **Subscribers** tab. It cannot create, edit, publish or delete contracts, cannot validate
a contract, cannot read the audit log and cannot grant roles.

The role list also has `notifier`. It is a service role, not for people: identity grants it at
Root to its own notifier service account, so that notices can be sent by email and the existing webhook hosts can be copied once into the allowed-hosts list. Do not grant it to
anyone else.

- In the web app, open the folder and its **Access** tab.
- Through the API, call `POST /api/v1/identity/folders/{folder_id}/grants` with `principal_type`
  (`user`, `local_group`, `entra_group`, `service_principal` or `service_account`), either
  `principal_id` or (for a local user) `username`, and `role`.

Granting needs an interactive sign-in, so a token or key cannot change grants. You can only grant
the roles the table lists for your role, so an Organization, Project or Team admin never grants
its own role or a higher one. Nobody can grant a role to themselves. The folder's **Access** tab
also shows the effective access: who can do what in that folder, including inherited grants.

A good start: give one person `org_admin` on each Organization, and let them create projects,
teams and grants below it. Keep the number of Global admins small.

## Access for pipelines

The CLI, the Python SDK and pipelines sign in with tokens, not passwords:

- **Personal access token:** a person creates one in the account menu under **Access tokens** (or
  `POST /api/v1/identity/pats`). It acts with that person's access, optionally read-only, and
  always has an expiry date.
- **Service account and key:** for CI and other machines. Create the account under
  **Administration > Service accounts** (or `POST /api/v1/identity/service-accounts` with
  `owner_folder_id` and `name`), create a key for it, and grant the account a role like any other
  principal. An account holds at most two keys, so you can rotate without downtime.
- **Entra service principal:** with Entra connected, a registered service principal can use its
  own Entra access token.

A token or key is shown once, when it is created. Its lifetime is limited by the access-token
ceiling in [Runtime settings](settings.md). How data engineers use them is in
[the SDK and CLI guide](../user/sdk-cli.md).

## Next steps

- Connect single sign-on: [Microsoft Entra ID setup](entra-setup.md).
- Review the defaults an admin can change at runtime: [Runtime settings](settings.md).
- Plan backups before real data arrives: [Upgrade, roll back, back up and restore](upgrade-backup.md).
