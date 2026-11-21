"""A respx stand-in for the Contractum read API: contracts, versions, documents and folders."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import httpx
import respx

BASE = "https://contractum.example"
CONTRACTS = "/api/v1/contracts/contracts"
FOLDERS = "/api/v1/identity/folders"
YAML = "application/yaml"
PROBLEM = "application/problem+json"
_NOW = "2026-11-21T09:30:15.123456Z"
_FOLDER_ID = "11111111-2222-4333-8444-555555555555"


def _precedence(text: str) -> tuple[tuple[int, ...], int, str]:
    core, _, pre = text.partition("+")[0].partition("-")
    return tuple(int(part) for part in core.split(".")), 0 if pre else 1, pre


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass
class FakeVersion:
    """One version of a fake contract; ``served`` replaces the body to simulate tampering."""

    semver: str
    content: bytes
    status: str = "older"
    deprecated: bool = False
    sunset_date: str | None = None
    media_type: str = YAML
    served: bytes | None = None
    etag: str | None = None
    report: dict[str, Any] | None = None
    not_modified: bool = False
    headers_only: bool = False
    """Deprecation and sunset appear in headers but not in the metadata."""

    @property
    def digest(self) -> str:
        return sha256(self.content)


@dataclass
class FakeContract:
    contract_id: str
    odcs_id: str
    name: str
    folder_id: str
    versions: list[FakeVersion] = field(default_factory=list)
    domain: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str | None = "active"

    def ordered(self) -> list[FakeVersion]:
        return sorted(self.versions, key=lambda v: _precedence(v.semver), reverse=True)

    def latest(self) -> FakeVersion | None:
        return next((v for v in self.versions if v.status == "latest"), None)


def _version_json(contract: FakeContract, version: FakeVersion) -> dict[str, Any]:
    return {
        "content_sha256": version.digest,
        "contract_id": contract.contract_id,
        "deprecated": version.deprecated and not version.headers_only,
        "deprecated_at": _NOW if version.deprecated and not version.headers_only else None,
        "engine_version": "1.0.0",
        "etag": f'"{version.digest}"',
        "fetch_url": f"{CONTRACTS}/{contract.contract_id}/versions/{version.semver}",
        "lifecycle": "published",
        "media_type": version.media_type,
        "odcs_api_version": "v3.0.2",
        "pinned_rule_sets": [],
        "published_at": _NOW,
        "published_by": "dana",
        "schema_sha": "abc123",
        "semver": version.semver,
        "size_bytes": len(version.content),
        "status": version.status,
        "sunset_date": None if version.headers_only else version.sunset_date,
        "validation_run_id": None,
        "version_id": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{contract.contract_id}/{version.semver}")
        ),
    }


def _contract_json(contract: FakeContract) -> dict[str, Any]:
    latest = contract.latest()
    return {
        "api_version": "v3.0.2",
        "content_redacted": False,
        "content_sha256": latest.digest if latest else None,
        "created_at": _NOW,
        "created_by": "dana",
        "domain": contract.domain,
        "folder_id": contract.folder_id,
        "id": contract.contract_id,
        "latest_version": (
            {
                "semver": latest.semver,
                "version_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{contract.contract_id}/{latest.semver}")
                ),
            }
            if latest
            else None
        ),
        "media_type": YAML,
        "name": contract.name,
        "notation": "CT-1",
        "odcs_id": contract.odcs_id,
        "organization_id": None,
        "origin": None,
        "size_bytes": len(latest.content) if latest else None,
        "status": contract.status,
        "tags": contract.tags,
        "template": None,
        "updated_at": _NOW,
        "updated_by": "dana",
        "version": {"number": len(contract.versions), "state": "published"},
    }


def _problem(status: int, slug: str, title: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": PROBLEM},
        json={"type": f"urn:dc:problem:{slug}", "title": title, "status": status},
    )


class FakeContractum:
    """Register contracts, then use it as a context manager; every request is recorded."""

    def __init__(self, base_url: str = BASE) -> None:
        self.base_url = base_url
        self.contracts: list[FakeContract] = []
        self.folders: dict[str, str] = {}
        self.page_size: int | None = None
        self.loose_odcs_filter = False
        self.diff_status = 200
        """The status of the diff route; anything but 200 answers with a problem body."""
        self.diff_changes: list[dict[str, Any]] = [
            {
                "code": "property.type.changed",
                "class": "breaking",
                "element_kind": "property",
                "path": "orders.quantity",
                "attribute": "logicalTypeOptions.exclusiveMaximum",
                "before": None,
                "after": 3,
            },
            {"code": "description.changed", "class": "info", "path": "orders"},
        ]
        self._snapshot: list[httpx.Request] | None = None
        self._router = respx.mock(base_url=base_url, assert_all_called=False)
        self._register()

    # --- building -------------------------------------------------------------------------

    def add_folder(self, path: str, folder_id: str | None = None) -> str:
        identifier = folder_id or str(uuid.uuid5(uuid.NAMESPACE_URL, path))
        self.folders[path] = identifier
        return identifier

    def add_contract(
        self,
        odcs_id: str,
        versions: list[FakeVersion] | None = None,
        *,
        name: str | None = None,
        folder_id: str = _FOLDER_ID,
        contract_id: str | None = None,
        domain: str | None = None,
        tags: list[str] | None = None,
    ) -> FakeContract:
        contract = FakeContract(
            contract_id=contract_id
            or str(uuid.uuid5(uuid.NAMESPACE_URL, f"{folder_id}/{odcs_id}")),
            odcs_id=odcs_id,
            name=name or odcs_id.title(),
            folder_id=folder_id,
            versions=list(versions or []),
            domain=domain,
            tags=list(tags or []),
        )
        self.contracts.append(contract)
        return contract

    # --- recording ------------------------------------------------------------------------

    @property
    def requests(self) -> list[httpx.Request]:
        if self._snapshot is not None:
            return self._snapshot
        return [call.request for call in self._router.calls]

    @property
    def router(self) -> respx.MockRouter:
        """The underlying router, for tests that add routes of their own (auth endpoints)."""
        return self._router

    def targets(self) -> list[str]:
        """Method, path and query of every request, in order."""
        return [f"{r.method} {r.url.raw_path.decode()}" for r in self.requests]

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def __enter__(self) -> FakeContractum:
        self._snapshot = None
        self._router.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._snapshot = [call.request for call in self._router.calls]
        self._router.stop()

    # --- routes ---------------------------------------------------------------------------

    def _register(self) -> None:
        pattern = re.escape(CONTRACTS) + r"/(?P<cid>[^/]+)"
        self._router.get(path=CONTRACTS).mock(side_effect=self._list)
        self._router.get(path=FOLDERS).mock(side_effect=self._folders)
        self._router.get(path__regex=f"^{pattern}$").mock(side_effect=self._contract)
        self._router.get(path__regex=f"^{pattern}/versions$").mock(side_effect=self._versions)
        self._router.get(path__regex=f"^{pattern}/versions/(?P<ref>[^/]+)$").mock(
            side_effect=self._document
        )
        self._router.get(path__regex=f"^{pattern}/versions/(?P<ref>[^/]+)/metadata$").mock(
            side_effect=self._metadata
        )
        self._router.get(path__regex=f"^{pattern}/versions/(?P<ref>[^/]+)/validation-report$").mock(
            side_effect=self._validation_report
        )
        self._router.get(path__regex=f"^{pattern}/diff$").mock(side_effect=self._diff)

    def _find(self, cid: str) -> FakeContract | None:
        return next((c for c in self.contracts if c.contract_id == cid), None)

    def _pick(self, contract: FakeContract, ref: str) -> FakeVersion | None:
        if ref == "latest":
            return contract.latest()
        return next((v for v in contract.versions if v.semver == ref), None)

    def _list(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params
        items = [
            c
            for c in self.contracts
            if (self.loose_odcs_filter or not query.get("odcs_id") or c.odcs_id == query["odcs_id"])
            and (not query.get("folder_id") or c.folder_id == query["folder_id"])
            and (not query.get("domain") or c.domain == query["domain"])
            and (not query.get("status") or c.status == query["status"])
            and all(tag in c.tags for tag in query.get_list("tags"))
            and (not query.get("q") or query["q"].lower() in f"{c.name} {c.odcs_id}".lower())
        ]
        start = int(query["cursor"].removeprefix("c")) if query.get("cursor") else 0
        size = self.page_size or int(query.get("limit", "50"))
        page = items[start : start + size]
        more = start + size < len(items)
        return httpx.Response(
            200,
            json={
                "items": [_contract_json(c) for c in page],
                "next_cursor": f"c{start + size}" if more else None,
            },
        )

    def _folders(self, request: httpx.Request) -> httpx.Response:
        path = request.url.params.get("path", "")
        items = [
            {
                "archived": False,
                "archived_at": None,
                "block_inheritance": False,
                "created_at": _NOW,
                "created_by": "admin",
                "display_name": slug.rsplit("/", 1)[-1].title(),
                "id": folder_id,
                "kind": "team",
                "parent_id": None,
                "path": slug,
                "slug": slug.rsplit("/", 1)[-1],
                "updated_at": _NOW,
                "updated_by": "admin",
            }
            for slug, folder_id in self.folders.items()
            if slug == path
        ]
        return httpx.Response(200, json={"items": items, "next_cursor": None})

    def _contract(self, request: httpx.Request, cid: str) -> httpx.Response:
        contract = self._find(cid)
        if contract is None:
            return _problem(404, "not-found", "Contract not found")
        return httpx.Response(200, json=_contract_json(contract))

    def _versions(self, request: httpx.Request, cid: str) -> httpx.Response:
        contract = self._find(cid)
        if contract is None:
            return _problem(404, "not-found", "Contract not found")
        return httpx.Response(
            200, json={"items": [_version_json(contract, v) for v in contract.ordered()]}
        )

    def _metadata(self, request: httpx.Request, cid: str, ref: str) -> httpx.Response:
        contract = self._find(cid)
        version = self._pick(contract, ref) if contract else None
        if contract is None or version is None:
            return _problem(404, "not-found", "Version not found")
        return httpx.Response(200, json=_version_json(contract, version))

    def _document(self, request: httpx.Request, cid: str, ref: str) -> httpx.Response:
        contract = self._find(cid)
        version = self._pick(contract, ref) if contract else None
        if contract is None or version is None:
            return _problem(404, "not-found", "Version not found")
        if version.not_modified:
            return httpx.Response(304)
        etag = version.etag or f'"{version.digest}"'
        headers = {"content-type": version.media_type, "etag": etag}
        if version.deprecated:
            headers["deprecation"] = "@1795000000"
        if version.sunset_date:
            headers["sunset"] = "Sun, 31 Jan 2027 00:00:00 GMT"
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(
                304, headers={k: v for k, v in headers.items() if k != "content-type"}
            )
        body = version.served if version.served is not None else version.content
        return httpx.Response(200, headers=headers, content=body)

    def _diff(self, request: httpx.Request, cid: str) -> httpx.Response:
        contract = self._find(cid)
        if contract is None or self.diff_status == 404:
            return _problem(404, "not-found", "Contract not found")
        if self.diff_status != 200:
            return _problem(self.diff_status, "forbidden", "Not allowed")
        sides = {}
        for name in ("from", "to"):
            version = self._pick(contract, request.url.params[name])
            if version is None:
                return _problem(404, "not-found", "Version not found")
            sides[name] = {
                "content_sha256": version.digest,
                "kind": "version",
                "ref": request.url.params[name],
                "semver": version.semver,
                "version_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{cid}/{version.semver}")),
            }
        counts: dict[str, int] = {}
        for change in self.diff_changes:
            counts[change["class"]] = counts.get(change["class"], 0) + 1
        return httpx.Response(
            200,
            json={
                "from": sides["from"],
                "to": sides["to"],
                "changes": self.diff_changes,
                "changes_total": len(self.diff_changes),
                "counts": counts,
                "required_bump": "major" if "breaking" in counts else "minor",
                "classifier_version": "1",
                "truncated": False,
                "next_cursor": None,
            },
        )

    def _validation_report(self, request: httpx.Request, cid: str, ref: str) -> httpx.Response:
        contract = self._find(cid)
        version = self._pick(contract, ref) if contract else None
        if contract is None or version is None or version.report is None:
            return _problem(404, "not-found", "Report not found")
        return httpx.Response(
            200,
            json={
                "content_sha256": version.digest,
                "contract_id": contract.contract_id,
                "dsl_version": "cel-2",
                "engine_version": "1.0.0",
                "pins": [],
                "report": version.report,
                "run_id": "b1b2c3d4-0000-4000-8000-000000000002",
                "schema_sha": "abc123",
                "semver": version.semver,
                "validated_at": _NOW,
                "version_id": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{contract.contract_id}/{version.semver}")
                ),
            },
        )
