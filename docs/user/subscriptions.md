# Subscriptions

A subscriber card tells your team when a contract you depend on changes. This page is for data
engineers who want that notice before a change reaches their pipeline.

Contents: [What a card is](#what-a-subscriber-card-is) | [Who can create one](#who-can-create-a-card) | [Members](#members-and-who-can-do-what) | [Subscribing and pins](#subscribing-and-pins) | [Events](#which-events-send-a-notice) | [Channels](#channels-email-and-webhook) | [Notices tab](#the-notices-tab) | [SDK and CLI](#sdk-and-cli) | [Typical flow](#a-typical-flow)

## What a subscriber card is

A subscriber card records which published contract versions a team depends on. Contractum then
tells the card when such a contract gets a new version, a new Latest, a deprecation, a sunset
reminder, or an attempt to unpublish the version.

Each dependency is a **pin**: one card, one contract, one published version. A pin only records the
dependency. It never gives anyone access to the contract. To read the contract, you still need your
own role on its folder.

In the web app, cards live under **Subscriptions** in the navigation, and in each folder's
**Subscribers** tab.

## Who can create a card

A card belongs to one folder of any kind: Root, Organization, Project or Team. The card's name must
be unique among the active cards of that folder.

You can create a card in a folder where you hold one of these roles, on that folder or on a folder
above it:

- **Consumer**
- **Team admin**, **Project admin**, **Organization admin** or **Global admin**

Readers, editors and Policy managers can read cards but cannot create them. See the
[roles table](../admin/first-run.md#grant-access) for what each role allows.

To create a card, open **Subscriptions** (or a folder's **Subscribers** tab) and choose
**Create card**. Give it a name, a description and up to 20 contacts. A contact is an email
address, a URL or a line of text.

## Members and who can do what

| Who | Read the card | Change its subscriptions | Edit, archive, members, webhook |
|-----|---------------|--------------------------|---------------------------------|
| The card's creator, while they can still read the folder | Yes | Yes | Yes |
| An admin of the folder or a folder above it (the roles that can create cards, except Consumer) | Yes | Yes | Yes |
| A member of the card | Yes | Yes | No |
| Anyone else who can read the folder | Yes | No | No |
| Everyone else | No | No | No |

- **Members** are local groups, Entra groups and service accounts. A manager links them on the
  **Members** tab. Contractum checks that the group or account may be linked. People cannot be
  linked one by one: link a group instead.
- A member can read the card and subscribe, move or unsubscribe it, even without a role on the
  card's folder. A member can never edit the card, archive it, change its members or touch its
  webhook.
- For someone who cannot read the card, it does not exist: it is not listed and its page answers
  "Not found".
- **Archive card** ends all its pins and stops its notices. Notices still waiting to be sent are
  skipped. An archived card cannot be changed or restored.

## Subscribing and pins

On the card's **Subscriptions** tab, choose **Subscribe to a contract**. Find the contract, then pick
one of its published versions.

- A pin names one exact version, such as `1.4.0`. It never follows Latest. You can pin a Preview or
  an older version, as long as it is published and not deprecated.
- You must be able to read that published version yourself. The card gives you no extra access.
- A deprecated version cannot be newly pinned. Pins made before the deprecation stay.
- **Move** moves a pin to another published version of the same contract. **Unsubscribe** ends a pin.
  Ended pins stay on the tab, with who ended them and why. Pins are never deleted.

**Pins protect the version.** While a card pins a version, the contract owner cannot unpublish that
version (`version-pinned`) or delete the contract (`contract-pinned`). The refusal names the cards
that pin it. A refused unpublish also sends those cards a notice. See the
[subscriber card errors](api-guide.md#subscriber-cards) and the list of
[problem types](api-guide.md#errors-rfc-9457-problem-details).

**The owner's side.** People who can read the full contract see its **Subscribers** tab. It lists
the cards with an active pin per version: name, description, contacts and when it was pinned. It
never shows members, channels or a card's other pins. People who can publish the contract can also
choose **Remove subscription**, with a reason. That ends the pin and sends the card a notice with the
reason. A Consumer does not see this tab.

## Which events send a notice

| Event | Which cards are told | When |
|-------|----------------------|------|
| `contract.version.published` | Cards that pin any version of the contract | A new version is published |
| `contract.version.breaking_published` | Cards whose pinned version the new version breaks | With the publish notice, as an extra notice. It lists the breaking changes per pinned version (at most 50 each) |
| `contract.latest.changed` | Cards that pin any version of the contract | A version is promoted to Latest |
| `contract.version.deprecated` | Cards that pin that version | The version is deprecated, with its sunset date |
| `contract.version.sunset_approaching` | Cards that pin that deprecated version | A set number of days before the sunset date: 30 and 7 by default |
| `contract.version.unpublish_blocked` | Cards that pin that version | The owner tried to unpublish it and was refused. At most once a day per card and version, by default |
| `card.subscription.removed` | The card whose pin was removed | A contract owner removed the pin. The notice carries the reason |

Only active pins count, and archived cards are never told. A notice carries identifiers, versions
and event fields. It never carries contract content, contacts or members. A Global admin can change
the sunset reminder days in [Runtime settings](../admin/settings.md#notifications).

## Channels: email and webhook

Every notice is listed on the card's **Notices** tab. It is also sent through each channel that was
on when the notice was recorded.

### Email

Tick **Email notices to the email contacts** when you create or edit the card. Each notice then goes
to every email contact on the card at the time it is sent. Contractum sends it through its own mail
service. A Global admin sets the sender and the email texts in
[Runtime settings](../admin/settings.md#notifications).

### Webhook

You set up a webhook after the card exists. Open the card, choose **Edit card**, tick
**Deliver notices to a webhook** and enter the **Webhook URL**. Only the card's creator and the folder
admins can do this.

The URL must meet these rules:

- It uses HTTPS.
- Its host is a name, not an IP address, and it has no user name or password in it.
- The host resolves only to public addresses, unless an operator has listed it as an allowed private host. Contractum checks this again before every delivery.
- The host is on the list of allowed webhook hosts. A Global admin keeps that list under
  **Administration > Settings > Notifications**. An empty list allows no host. If your host is
  missing, the card form says so. Ask a Global admin to add it.

People who are not managers of the card see only the scheme and host of its URL.

**The signing secret.** When you first turn the webhook on, Contractum creates a signing secret
(`whsec_...`) and shows it **once**. Store it in your receiver's secret store right away; it is never
shown again. **Rotate secret** on the card's **Webhook** tab creates a new secret, also shown once.
The previous secret keeps working for an overlap window (one day by default), so you can update the
receiver without missing a delivery. Creating and rotating the secret need an interactive sign-in:
a personal access token or a service-account key is refused.

**Each request.** Contractum sends one `POST` with a JSON body
`{"type": ..., "timestamp": ..., "data": {...}}`. `type` is the event from the table above.
`data` holds the notice fields plus `schema_version`, `notice_id` and the card's `id` and `name`.
The request follows the Standard Webhooks format and carries these headers:

| Header | Value |
|--------|-------|
| `webhook-id` | `msg_<delivery id>`. The same on every retry of one delivery, so use it to drop duplicates |
| `webhook-timestamp` | The time of this attempt, in Unix seconds |
| `webhook-signature` | `v1,<base64 HMAC-SHA256>`. During an overlap window there are two entries, separated by a space |

**Check the signature.** The key is the base64-decoded part of the secret after `whsec_`. The
signed text is `<webhook-id>.<webhook-timestamp>.<raw body>`. Accept the request when any `v1`
entry matches. Any Standard Webhooks library does this for you. In plain Python:

```python
import base64
import hashlib
import hmac


def verify(secret: str, headers: dict[str, str], body: bytes) -> bool:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(
        hmac.compare_digest(entry.removeprefix("v1,"), expected)
        for entry in headers["webhook-signature"].split()
    )
```

Use the raw body bytes, not a parsed and re-encoded copy. Also refuse a timestamp that is far from
your own clock, for example more than five minutes.

Answer with any 2xx status to confirm the delivery. Contractum does not follow redirects, so a 3xx
answer counts as a failure, like a 4xx, a 5xx, a timeout or a TLS error.

## The Notices tab

The card's **Notices** tab lists every notice, newest first. Each row shows the time, the event kind,
a link to the contract, a short message (such as the version or the reason) and one badge per
channel, for example **Webhook: delivered**. **In-app only** means no channel was on when the notice
was recorded.

| Delivery state | Meaning |
|----------------|---------|
| `pending` | Not sent yet, or waiting for its next retry |
| `delivered` | The receiver answered 2xx, or the mail service accepted the email |
| `failed` | Every attempt failed, or the failure cannot be fixed by a retry |
| `skipped` | Not sent: the channel was turned off, the card has no email contact, or the card was archived |

**Retries.** A failed attempt is retried after 1 minute, then 5 minutes, 30 minutes, 2 hours,
6 hours and 12 hours, each a little earlier or later at random. After 7 attempts the delivery is
`failed`. These are the defaults; a Global admin can change the schedule and the number of attempts
in [Runtime settings](../admin/settings.md#notifications). A webhook host that is not on the allowed
list fails at once, with no retry.

A failed delivery affects only that one delivery. The channel stays on, and the next notice is sent
again. Besides the state, the notices API (`GET /api/v1/contracts/cards/{card_id}/notices`) returns
the number of attempts, the last HTTP status and a short error code such as `timeout`, `http-503` or
`webhook-host-not-allowed`. It never returns the receiver's answer.

## SDK and CLI

The Contractum SDK and CLI only read contracts. They cannot create, edit or subscribe cards; use the
web app or the API for that.

They still warn a pipeline about the same changes when it fetches a contract
(see [SDK and CLI](sdk-cli.md)):

- `contractum versions <contract>` shows every version with its deprecation and sunset date.
- `contractum pull --fail-on-deprecated` and `contractum pull --sunset-within DAYS` stop a job with
  exit code 1 when a pulled version is deprecated or close to its sunset.
- The Python SDK emits a `ContractDeprecationWarning` when it fetches a deprecated version.

The `allow-no-subscriptions` line in the GitHub Actions example on the SDK and CLI page is an Azure
sign-in setting about Azure subscriptions. It has nothing to do with subscriber cards.

## A typical flow

1. Ask an admin for the **Consumer** role on the folder that holds the contracts your team uses, or
   on a folder above it.
2. Open **Subscriptions** and choose **Create card** in your team's folder. Add your team's email
   address as a contact and tick **Email notices to the email contacts**.
3. On the card's **Members** tab, a manager links your team's group, so your teammates can read the
   card and manage its subscriptions too.
4. On the **Subscriptions** tab, choose **Subscribe to a contract** and pin the version your pipeline
   uses.
5. Optional: choose **Edit card**, turn on the webhook with your receiver's URL, and store the
   signing secret it shows once.
6. When the owner publishes, promotes or deprecates a version, a notice appears on the
   **Notices** tab and goes to your email and webhook.
7. When you move your pipeline to a new version, **Move** the pin to it.
