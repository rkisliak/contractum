"""Card request models and card helpers that need no database (SUB-01, D-97, D-102)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from dc_auth.authorizer import AuthorizerUnavailable, Decision, FolderScope, Outcome
from dc_auth.principal import Principal
from dc_contracts import cards
from dc_contracts.api.cards import CardContactIn, SubscriberCardPatchIn, _public_webhook_url
from dc_contracts.card_index import CardIndexFilters, CardIndexScopes, list_visible_cards
from dc_contracts.cards import card_capability, card_has_member, map_card_integrity_error
from dc_contracts.models import UNIQUE_CARD_NAME_INDEX
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.unit


class _NoSession:
    """A session that must never be asked anything: the answer is known without a query."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no query expected")


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("email", "first..last@example.test"),
        ("url", "https://[::1/hook"),
        ("url", "https://hooks.example.test/a b"),
    ],
)
def test_a_contact_value_that_is_not_valid_for_its_kind_is_refused(kind: str, value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        CardContactIn(kind=kind, value=value)  # type: ignore[arg-type]
    (error,) = caught.value.errors()
    assert error["loc"] == ("value",)
    assert value not in error["msg"]  # fixed messages, never the submitted value (D-97)


def test_a_contact_name_with_a_control_character_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        CardContactIn(name="ops\x07team", kind="text", value="call us")
    (error,) = caught.value.errors()
    assert error["loc"] == ("name",)
    assert "control characters" in error["msg"]


@pytest.mark.parametrize("field", ["description", "email_enabled", "webhook_url"])
def test_a_patch_field_that_is_present_must_not_be_null(field: str) -> None:
    with pytest.raises(ValidationError, match="fields that are present must not be null"):
        SubscriberCardPatchIn.model_validate({field: None})


def test_an_absent_patch_field_is_simply_not_a_change() -> None:
    assert SubscriberCardPatchIn.model_validate({"email_enabled": True}).changes() == {
        "email_enabled": True
    }


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://[2001:db8::1]:8443/hook?token=secret", "https://[2001:db8::1]"),
        ("https://hooks.example.test/path?token=secret", "https://hooks.example.test"),
        (None, None),
    ],
)
def test_the_public_webhook_url_is_scheme_and_host_only(url: str | None, shown: str | None) -> None:
    assert _public_webhook_url(url) == shown


async def test_a_caller_with_no_grantee_identity_is_no_member() -> None:
    assert await card_has_member(_NoSession(), uuid.uuid4(), frozenset()) is False  # type: ignore[arg-type]


def _integrity_error(constraint: str | None) -> IntegrityError:
    orig = SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint))
    return IntegrityError("INSERT", {}, orig)  # type: ignore[arg-type]


def test_only_the_card_name_index_maps_to_card_name_taken() -> None:
    mapped = map_card_integrity_error(_integrity_error(UNIQUE_CARD_NAME_INDEX))
    assert mapped is not None
    assert (mapped.status, mapped.slug) == (409, "card-name-taken")
    assert map_card_integrity_error(_integrity_error("fk_card_member_card")) is None
    assert map_card_integrity_error(_integrity_error(None)) is None


async def test_a_caller_who_can_read_no_folder_and_has_no_identity_sees_an_empty_index() -> None:
    page = await list_visible_cards(
        _NoSession(),  # type: ignore[arg-type]
        CardIndexScopes(folder_read=FolderScope(all=False), grantees=frozenset()),
        CardIndexFilters(),
    )
    assert (page.rows, page.next_cursor, page.total_cards, page.total_folders) == ([], None, 0, 0)


PRINCIPAL = Principal(
    id="fake:reader",
    type="user",
    display="reader",
    issuer="https://issuer.test",
    subject="reader",
    via="jwt",
    scopes=frozenset({"api"}),
)


def _card() -> Any:
    return SimpleNamespace(id=uuid.uuid4(), folder_id=uuid.uuid4(), created_by="fake:creator")


class _FolderOnly:
    """An authorizer that answers the folder check only (no grantee listing)."""

    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome
        self.checks: list[str] = []

    async def check(self, principal: Any, permission: str, folder_id: Any = None, **_: Any) -> Any:
        self.checks.append(permission)
        return Decision(outcome=self.outcome)

    async def accessible_folders(self, principal: Any, permission: str) -> FolderScope:
        raise AssertionError("not asked without grantees")


class _FlakyGrantees(_FolderOnly):
    """Lists the caller's grantees only on the second ask, as identity recovering would."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome)
        self.asked = 0

    async def grantees(self, principal: Any) -> frozenset[tuple[str, str]]:
        self.asked += 1
        if self.asked == 1:
            raise AuthorizerUnavailable("identity restarting")
        return frozenset({("user", principal.id)})

    async def check_members(self, *_args: Any) -> dict[tuple[str, str], bool]:
        raise AssertionError("not asked by card_capability")


async def test_without_grantee_listing_the_folder_check_alone_decides_a_read() -> None:
    authorizer = _FolderOnly(Outcome.ALLOW)
    capability = await card_capability(
        authorizer,  # type: ignore[arg-type]
        PRINCIPAL,
        _card(),
        _NoSession(),  # type: ignore[arg-type]
        need="read",
    )
    assert (capability.read, capability.subscribe, capability.manage) == (True, False, False)
    assert authorizer.checks == ["folder:read"]


async def test_grantees_unreadable_at_first_are_asked_again_after_the_folder_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def linked(session: Any, card_id: Any, grantees: frozenset[tuple[str, str]]) -> bool:
        return grantees == frozenset({("user", PRINCIPAL.id)})

    monkeypatch.setattr(cards, "card_has_member", linked)
    authorizer = _FlakyGrantees(Outcome.NOT_FOUND)
    capability = await card_capability(
        authorizer,  # type: ignore[arg-type]
        PRINCIPAL,
        _card(),
        _NoSession(),  # type: ignore[arg-type]
        need="read",
    )
    assert (capability.read, capability.subscribe, capability.manage) == (True, True, False)
    assert capability.facts is None
    assert authorizer.asked == 2
