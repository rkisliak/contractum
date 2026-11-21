"""Pins the exact publish and subscribe allow list of every NATS user in the template (T-04.1-38).

Set equality, not containment: a subject added to any user fails here and has to be argued for in
review. The contracts user may run exactly one durable consumer (``contracts-notices``) on stream
AUDIT with the filter ``audit.contracts.>``; the filter token is part of the create subject, so the
server refuses a body filter that differs from it. A create subject without that token, the legacy
durable-create subject or any stream API subject would let contracts read every service's events
or alter the stream, so none may appear (the integration test against a real server proves the
refusals; this test keeps the list from drifting). Since Phase 7 (D-171) contracts also publishes
its best-effort access events on ``access.contracts.>`` (stream ACCESS, owned by the audit service);
no other ``access.`` subject, so it can never write another service's access events.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEMPLATE = Path(__file__).resolve().parents[2] / "infra" / "nats" / "nats.conf.tmpl"

CONTRACTS_PUBLISH = {
    "audit.contracts.>",
    "access.contracts.>",
    "$JS.API.CONSUMER.INFO.AUDIT.contracts-notices",
    "$JS.API.CONSUMER.CREATE.AUDIT.contracts-notices.audit.contracts.>",
    "$JS.API.CONSUMER.MSG.NEXT.AUDIT.contracts-notices",
    "$JS.ACK.AUDIT.contracts-notices.>",
}

EXPECTED: dict[str, tuple[set[str], set[str]]] = {
    "identity": ({"audit.identity.>"}, {"_INBOX.>"}),
    "contracts": (CONTRACTS_PUBLISH, {"_INBOX.>"}),
    "validation": ({"audit.validation.>"}, {"_INBOX.>"}),
    "audit": ({"$JS.API.>", "$JS.ACK.>"}, {"_INBOX.>"}),
}


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _users() -> dict[str, tuple[set[str], set[str]]]:
    """Parse the ``users`` block into {user: (publish allow, subscribe allow)}."""
    text = _strip_comments(TEMPLATE.read_text(encoding="utf-8"))
    users: dict[str, tuple[set[str], set[str]]] = {}
    for block in re.split(r"\n\s*\{\s*\n\s*user:", text)[1:]:
        name = re.match(r'\s*"([^"]+)"', block)
        publish = re.search(r"publish:\s*\{\s*allow:\s*\[([^\]]*)\]", block)
        subscribe = re.search(r"subscribe:\s*\{\s*allow:\s*\[([^\]]*)\]", block)
        assert name and publish and subscribe, block
        users[name.group(1)] = (
            set(re.findall(r'"([^"]+)"', publish.group(1))),
            set(re.findall(r'"([^"]+)"', subscribe.group(1))),
        )
    return users


def test_the_template_names_exactly_the_four_service_users() -> None:
    assert set(_users()) == set(EXPECTED)


@pytest.mark.parametrize("user", sorted(EXPECTED))
def test_each_user_has_exactly_its_pinned_allow_lists(user: str) -> None:
    publish, subscribe = _users()[user]
    expected_publish, expected_subscribe = EXPECTED[user]
    assert publish == expected_publish
    assert subscribe == expected_subscribe


def test_only_the_audit_user_holds_the_jetstream_api_wildcard() -> None:
    holders = {name for name, (publish, _) in _users().items() if "$JS.API.>" in publish}
    assert holders == {"audit"}


def test_no_contracts_subject_is_a_create_without_the_filter_token_or_a_stream_api() -> None:
    publish, _ = _users()["contracts"]
    create_prefix = "$JS.API.CONSUMER.CREATE.AUDIT.contracts-notices."
    for subject in publish:
        assert not subject.startswith("$JS.API.CONSUMER.DURABLE.CREATE"), subject
        assert not subject.startswith("$JS.API.STREAM."), subject
        assert not subject.startswith("$JS.API.DIRECT."), subject
        assert subject not in {"$JS.API.>", "$JS.API.*", "$JS.API.CONSUMER.>", "$JS.>", ">"}, (
            subject
        )
        if subject.startswith("$JS.API.CONSUMER.CREATE"):
            # the create subject carries the consumer name AND the filter token
            assert subject == create_prefix + "audit.contracts.>", subject


def test_the_template_keeps_its_password_placeholders() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for user in EXPECTED:
        assert "{{ " + user + "_nats_password }}" in text
