"""``recorded_refusal`` (WR-01): a refusal derived from a decision's permissions reaches the PDP.

The handler already holds an ALLOW decision for the route permission and refuses because that
decision lacks another permission. Without a PDP check of that permission no denial would be
written, so the helper asks for it at the decision's folder and then returns the uniform 404,
whatever the second check says; an Authorizer that cannot answer is 503.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from dc_auth.authorizer import AuthorizerUnavailable, Decision, FolderFacts, Outcome
from dc_auth.deps import recorded_refusal
from dc_auth.principal import Principal

pytestmark = pytest.mark.unit


class _RecordingAuthorizer:
    def __init__(self, outcome: Outcome | None) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, UUID | None, bool]] = []

    async def check(
        self,
        principal: Principal,
        permission: str,
        folder_id: UUID | None = None,
        *,
        fresh: bool = False,
    ) -> Decision:
        self.calls.append((principal.id, permission, folder_id, fresh))
        if self.outcome is None:
            raise AuthorizerUnavailable("down")
        return Decision(outcome=self.outcome)


def _principal(sub: str) -> Principal:
    return Principal(
        id=f"fake:{sub}", type="user", display=sub, issuer="fake", subject=sub, via="jwt"
    )


def _facts(folder_id: UUID) -> FolderFacts:
    return FolderFacts(
        id=folder_id,
        kind="team",
        slug="team",
        path="root.org.project.team",
        ancestor_ids=(folder_id,),
        organization_id=uuid4(),
        archived=False,
    )


def _request(authorizer: _RecordingAuthorizer, principal: Principal, folder_id: UUID) -> Any:
    decision = Decision(outcome=Outcome.ALLOW, folder=_facts(folder_id))
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(authorizer_factory=lambda _r: authorizer)),
        state=SimpleNamespace(principal=principal, decision=decision),
    )


@pytest.mark.parametrize("outcome", [Outcome.NOT_FOUND, Outcome.FORBIDDEN, Outcome.ALLOW])
async def test_the_decisive_permission_is_checked_and_the_answer_is_the_uniform_404(
    outcome: Outcome,
) -> None:
    folder = uuid4()
    authorizer = _RecordingAuthorizer(outcome)
    request = _request(authorizer, _principal("reader"), folder)

    problem = await recorded_refusal(request, "contract:read")

    assert authorizer.calls == [("fake:reader", "contract:read", folder, False)]
    assert (problem.status, problem.slug) == (404, "not-found")


async def test_an_explicit_folder_and_principal_win_over_the_routes_decision() -> None:
    authorizer = _RecordingAuthorizer(Outcome.NOT_FOUND)
    request = _request(authorizer, _principal("route"), uuid4())
    source_folder = uuid4()

    problem = await recorded_refusal(
        request, "contract:publish", source_folder, principal=_principal("explicit")
    )

    assert authorizer.calls == [("fake:explicit", "contract:publish", source_folder, False)]
    assert problem.status == 404


async def test_an_authorizer_that_cannot_answer_is_503_never_an_unrecorded_404() -> None:
    authorizer = _RecordingAuthorizer(None)
    request = _request(authorizer, _principal("reader"), uuid4())

    problem = await recorded_refusal(request, "ruleset:read")

    assert (problem.status, problem.slug) == (503, "service-unavailable")
