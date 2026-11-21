// Subscriber-card records for the card component tests.
import type { CardSubscription, SubscriberCard } from "@/api/types";

export function cardFixture(
  overrides: Partial<SubscriberCard> = {},
): SubscriberCard {
  return {
    id: "card-1",
    folder_id: "fraud-id",
    name: "demo-analytics-team",
    description: "Analytics team",
    contacts: [],
    email_enabled: false,
    webhook_enabled: false,
    webhook_url: null,
    webhook_secret_configured: false,
    archived_at: null,
    archived_by: null,
    created_by: "local:admin",
    created_at: "2026-11-01T10:00:00Z",
    updated_by: "local:admin",
    updated_at: "2026-11-01T10:00:00Z",
    members: [],
    subscriptions: [],
    subscriptions_truncated: false,
    ...overrides,
  };
}

export function subscriptionFixture(
  overrides: Partial<CardSubscription> = {},
): CardSubscription {
  return {
    id: "sub-1",
    card_id: "card-1",
    contract_id: "contract-1",
    version_id: "v-1",
    semver: "1.0.0",
    mode: "pinned",
    pinned_by: "local:admin",
    pinned_at: "2026-11-02T10:00:00Z",
    ended_by: null,
    ended_at: null,
    end_reason: null,
    end_note: null,
    replaced_by_id: null,
    ...overrides,
  };
}

/** A published contract version as `GET /contracts/{id}/versions` lists it. */
export function publishedVersionFixture(
  semver: string,
  id: string,
  extra: Record<string, unknown> = {},
) {
  return {
    version_id: id,
    contract_id: "contract-1",
    semver,
    lifecycle: "published",
    status: "older",
    deprecated: false,
    deprecated_at: null,
    sunset_date: null,
    content_sha256: "x",
    engine_version: null,
    etag: "e",
    fetch_url: "/x",
    media_type: "application/yaml",
    odcs_api_version: "v3.1.0",
    pinned_rule_sets: null,
    published_at: "2026-11-01T10:00:00Z",
    published_by: "local:admin",
    schema_sha: null,
    size_bytes: 1,
    validation_run_id: null,
    ...extra,
  };
}
