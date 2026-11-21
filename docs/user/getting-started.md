# Getting started

This page shows how to sign in, find a contract and fetch the version you need. It is for data
engineers, analysts and anyone else who reads contracts. In the examples,
`https://contractum.example.com` stands for the address of your Contractum server.

## Sign in

Open the Contractum address in your browser. The sign-in page offers the methods your
administrator turned on:

- **Username and password** for a local account. Your administrator creates the account and tells
  you the password.
- **Sign in with Microsoft** for your company Microsoft Entra ID account. This button appears only
  when an administrator has connected Entra ID.

If an account was created with a one-time password, Contractum asks for a new password right after
the first sign-in (the screen is called "Set a new password"). The first administrator account
works this way. To change the password of a local account later, open the account menu (your
initials), choose **Account settings**, then the **Password** tab.

After you sign in, **Home** shows who you are, your roles in each folder and links to the main
areas. You see only the folders, contracts and actions your roles allow. If something is missing,
ask the admin of that folder for access.

## Find a contract

Contracts live in a folder tree: Root, then Organization, Project and Team. There are three ways to
find one:

- **Folders** shows the tree. Open a folder, then its **Contracts** tab.
- **Contracts** lists every contract you can see, across all folders. Filter it by text, ODCS
  status or domain.
- The **Search contracts** box in the top bar searches from any page.

A contract page has tabs. Which tabs you see depends on your access:

| Tab | What it shows |
|-----|---------------|
| Overview | The ODCS id, folder, domain, ODCS status and the Latest version |
| Document | The current document |
| Versions | Every published version and how to fetch it |
| Diff | The classified changes between two versions |
| Drafts | Unpublished changes, for people who can read the full contract |
| Rule sets | The extra rule sets attached to this contract |
| Subscribers | Who is subscribed to changes of this contract |

## Fetch a version

Every published version has a lifecycle state:

- **Latest**: the current version. Most consumers use this one.
- **Preview**: a published version above the Latest. It is the upcoming version, so you can test
  your data against it before it becomes the Latest.
- **Older**: a version below the Latest. You can still fetch it.

A version can also be **deprecated**, sometimes with a sunset date. It can still be fetched, but you
should move to a newer one.

### In the web UI

Open the contract, then the **Versions** tab. The menu on each version row offers
**Copy fetch URL**, **Copy Latest alias URL**, **Copy curl example**, **Download YAML** and
**Download JSON**. The **How to fetch** card under the table shows the URLs for this contract.

### With curl

The Latest of a contract is always at `/api/v1/contracts/contracts/<contract-id>/versions/latest`.
Any other version, a Preview included, is at its own version number, for example
`.../versions/1.4.0`. The contract id is the UUID in the contract's page address. Send an access
token (see [Create an access token](#create-an-access-token)):

```bash
curl -H "Authorization: Bearer $TOKEN" -H "Accept: application/yaml" \
  https://contractum.example.com/api/v1/contracts/contracts/<contract-id>/versions/latest
```

Without an `Accept` header you get the exact bytes that were published, in the format they were
written in. `Accept: application/yaml` or `Accept: application/json` asks for that format. If it
differs from the stored one, you get a converted copy. To see every version with its state, call
`GET /api/v1/contracts/contracts/<contract-id>/versions`.

### With the CLI

The `contractum` command line names a contract by its ODCS id and an optional version:

```bash
contractum config set url https://contractum.example.com
printf '%s' "$TOKEN" | contractum login --token -
contractum versions payments.orders           # every version with its state
contractum show payments.orders               # the Latest
contractum show payments.orders@preview       # the upcoming Preview
contractum show payments.orders@1.4.0         # an exact version
contractum pull payments.orders@1.4.0         # save it and pin it in contractum.lock
```

[SDK and CLI](sdk-cli.md) explains how to install the CLI, the reference format and how to test
your data against a contract.

## Create an access token

Scripts, pipelines and the CLI sign in with a personal access token, never with your password.

1. Open the account menu (your initials) and choose **Access tokens**.
2. Choose **Create token**.
3. Give it a name that says where you use it, for example the pipeline name, and check the
   **Expires on** date. Tick **Read-only** if the token only needs to read. Every write with a read-only token is
   refused.
4. Copy the token right away. It is shown only once.

The token acts as you, with your access. Revoke it on the same page when you no longer need it.

## Next steps

- [SDK and CLI](sdk-cli.md): fetch contracts and test your data from the command line, Python,
  pytest or CI.
- [Subscriptions](subscriptions.md): for data engineers who want to be told before a contract they
  use changes, is deprecated or is retired.
- [API guide](api-guide.md): sign-in, URLs, errors, paging and versions for direct API calls.
- [UI guide](ui-guide.md): a tour of every screen by role.
- [Writing rules](writing-rules.md): for Policy Managers who write rule sets.
- [HTTP API reference](reference/http-api.md): every route with its permission.
