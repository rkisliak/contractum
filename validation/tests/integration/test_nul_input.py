"""A NUL (U+0000) or lone surrogate in rule-set input is a 422, never a 500 (Phase 7 post-fix).

Schemathesis found ``POST /folders/{folder_id}/rule-sets`` with ``name`` holding ``\\x00`` answered
500 (psycopg ``DataError``). The shared ``dc_core.nul_guard`` route guard now refuses it before the
database, and the raw-body draft routes refuse it after parsing (the draft is jsonb).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

NUL = "bad\x00name"
DETAIL = "NUL (U+0000) characters are not allowed"


def _assert_422(response: Any, slug: str, pointer: str) -> None:
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert body["errors"] == [{"pointer": pointer, "detail": DETAIL}]


async def test_nul_in_rule_set_input_is_a_422_and_nothing_is_stored(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()

        created = await stack.create_rule_set(chain.team, NUL)
        _assert_422(created, "validation-error", "/body/name")
        described = await stack.create_rule_set(chain.team, "Naming", description=NUL)
        _assert_422(described, "validation-error", "/body/description")
        assert stack.validation_sql("SELECT count(*) AS n FROM rule_set")[0]["n"] == 0

        listed = await stack.client.get(
            f"/api/v1/validation/folders/{chain.team}/rule-sets",
            params={"filter": NUL},
            headers=stack._headers(None),
        )
        _assert_422(listed, "validation-error", "/query/filter")

        ok = await stack.create_rule_set(chain.team, "Naming")
        assert ok.status_code == 201, ok.text
        rule_set_id = ok.json()["id"]
        draft = {
            "rules": [
                {
                    "key": "has-schema",
                    "condition": "has(this.schema)",
                    "severity": "error",
                    "message": NUL,
                    "fixtures": {
                        "pass": [{"document": {"schema": []}}],
                        "fail": [{"document": {}}],
                    },
                }
            ]
        }
        as_json = await stack.put_draft(
            rule_set_id, json.dumps(draft), '"r0"', content_type="application/json"
        )
        _assert_422(as_json, "malformed-document", "/rules/0/message")
        # YAML spells the NUL as the double-quoted escape "\0".
        as_yaml = await stack.put_draft(
            rule_set_id,
            'rules:\n  - key: has-schema\n    condition: "has(this.schema)"\n'
            '    severity: error\n    message: "bad\\0name"\n',
            '"r0"',
        )
        _assert_422(as_yaml, "malformed-document", "/rules/0/message")
        assert stack.validation_sql(
            "SELECT draft_revision AS r FROM rule_set WHERE id = %s", rule_set_id
        ) == [{"r": 0}]


SURROGATE_DETAIL = "Lone UTF-16 surrogate characters (U+D800 to U+DFFF) are not allowed"
# Submitted around the surrogate and searched for in the whole answer. x, q, h, o, p and r are not
# hex digits, so the marker never turns up by chance in the random request id or a UUID.
ECHO_MARKER = "xq-echo-probe"


def _assert_surrogate_422(response: Any, slug: str, pointer: str) -> None:
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert body["errors"] == [{"pointer": pointer, "detail": SURROGATE_DETAIL}]
    assert ECHO_MARKER not in response.text  # the submitted value is never echoed


async def test_lone_surrogate_in_rule_set_input_is_a_422_and_nothing_is_stored(
    make_stack: Any,
) -> None:
    """A lone UTF-16 surrogate (JSON ``"\\ud800"``) cannot be encoded for psycopg: 422, not 500."""
    async with make_stack() as stack:
        chain = await stack.chain()
        url = f"/api/v1/validation/folders/{chain.team}/rule-sets"
        headers = {**stack._headers(None), "content-type": "application/json"}

        # httpx cannot encode a lone surrogate itself, so the JSON escapes are sent as bytes.
        created = await stack.client.post(
            url,
            content=b'{"name": "xq-echo-probe\\ud800name", "description": null}',
            headers=headers,
        )
        _assert_surrogate_422(created, "validation-error", "/body/name")
        described = await stack.client.post(
            url,
            content=b'{"name": "Naming", "description": "xq-echo-probe\\uDFFF"}',
            headers=headers,
        )
        _assert_surrogate_422(described, "validation-error", "/body/description")
        assert stack.validation_sql("SELECT count(*) AS n FROM rule_set")[0]["n"] == 0

        ok = await stack.create_rule_set(chain.team, "Naming")
        assert ok.status_code == 201, ok.text
        rule_set_id = ok.json()["id"]
        as_json = await stack.put_draft(
            rule_set_id,
            b'{"rules": [{"key": "has-schema", "condition": "has(this.schema)",'
            b' "severity": "error", "message": "xq-echo-probe\\ud800name"}]}',
            '"r0"',
            content_type="application/json",
        )
        _assert_surrogate_422(as_json, "malformed-document", "/rules/0/message")
        as_yaml = await stack.put_draft(
            rule_set_id,
            'rules:\n  - key: has-schema\n    condition: "has(this.schema)"\n'
            '    severity: error\n    message: "xq-echo-probe\\ud800name"\n',
            '"r0"',
        )
        # libyaml (CSafeLoader) refuses the escape itself while scanning: still a 422.
        assert as_yaml.status_code == 422, as_yaml.text
        assert as_yaml.json()["type"] == "urn:dc:problem:malformed-document"
        assert ECHO_MARKER not in as_yaml.text
        assert stack.validation_sql(
            "SELECT draft_revision AS r FROM rule_set WHERE id = %s", rule_set_id
        ) == [{"r": 0}]
