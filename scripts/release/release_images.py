#!/usr/bin/env python3
"""The digest manifest of a release: which images were built, and which of them were tested.

    python3 scripts/release/release_images.py assemble --digests DIR --version V --commit SHA \
        --git-sha SHA12 --registry REG --output release-images.json
    python3 scripts/release/release_images.py record --manifest release-images.json \
        --run-id ID --e2e success --dast success --output tested-images.json
    python3 scripts/release/release_images.py check --manifest tested-images.json --version V \
        --commit SHA --registry REG --tested
    python3 scripts/release/release_images.py entries --manifest tested-images.json
    python3 scripts/release/release_images.py field --manifest tested-images.json version
    python3 scripts/release/release_images.py pin-chart --manifest tested-images.json \
        --values deploy/helm/contractum/values.yaml
    python3 scripts/release/release_images.py check-rendered --manifest tested-images.json \
        --version V --rendered rendered.yaml

The Release workflow (release.yml) builds the 7 server images once, as a release candidate
vX.Y.Z-rc.N, and writes their registry digests here (``assemble``). The full E2E and DAST then run
on exactly those digests, and only when both pass does ``record`` add the ``tested`` block. Promote
reads the same file back and adds the tags X.Y.Z and latest to those digests without a rebuild, so
the SBOM, the provenance and the attestations of the candidate stay valid for the stable image.

Every read is strict: an unknown or missing key, a malformed digest, commit, version or
repository, or a tag that is not ``v<version>`` stops with one line and exit code 1. The file is an
editable release asset, so nothing in it is trusted before it passes this check (and, in promote,
before its attestation verifies). Standard library only, so the workflow runs it with the runner's
own Python.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

IMAGES = ("identity", "contracts", "validation", "audit", "mail", "api-docs", "ui")
NAMESPACE = "contractum"
SCHEMA = 1
FIELDS = ("version", "tag", "commit", "git_sha", "registry")

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
GIT_SHA = re.compile(r"[0-9a-f]{12}")
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-rc\.[1-9]\d*)?")
REGISTRY = re.compile(
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
)
RUN_ID = re.compile(r"[0-9]{1,20}")

ANCHOR = "  imageDigests: {}"
IMAGE_LINE = re.compile(r"^\s*-?\s*image:\s*(?P<ref>\S+)\s*$")

TOP_KEYS = {"schema", "version", "tag", "commit", "git_sha", "registry", "images"}
TESTED_KEYS = {"run_id", "e2e", "dast"}


class ManifestError(Exception):
    """The manifest, or what was asked of it, is not acceptable; the message says why."""


def report(message: str) -> None:
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "error: "
    print(prefix + message, file=sys.stderr)


def repository(registry: str, name: str) -> str:
    return f"{registry}/{NAMESPACE}/{name}"


def _text(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ManifestError(f"the manifest {label} is missing or malformed")
    return value


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"the manifest {label} must be an object")
    keys = set(value)
    if keys != expected:
        extra = ", ".join(sorted(keys - expected))
        missing = ", ".join(sorted(expected - keys))
        detail = "; ".join(
            part
            for part in (
                f"unknown: {extra}" if extra else "",
                f"missing: {missing}" if missing else "",
            )
            if part
        )
        raise ManifestError(f"the manifest {label} has the wrong keys ({detail})")
    return value


def validate(data: object, *, allow_tested: bool = True) -> dict[str, Any]:
    """The whole strict check of a manifest; returns it when it passes."""
    if not isinstance(data, dict):
        raise ManifestError("the manifest must be a JSON object")
    expected = TOP_KEYS | ({"tested"} if "tested" in data and allow_tested else set())
    manifest = _exact_keys(data, expected, "top level")
    if manifest["schema"] != SCHEMA or isinstance(manifest["schema"], bool):
        raise ManifestError(f"the manifest schema must be {SCHEMA}")
    version = _text(manifest["version"], VERSION, "version")
    if manifest["tag"] != f"v{version}":
        raise ManifestError(f"the manifest tag must be v{version}")
    _text(manifest["commit"], COMMIT, "commit")
    _text(manifest["git_sha"], GIT_SHA, "git_sha")
    registry = _text(manifest["registry"], REGISTRY, "registry")
    images = _exact_keys(manifest["images"], set(IMAGES), "images")
    for name in IMAGES:
        entry = _exact_keys(images[name], {"repository", "digest"}, f"image {name}")
        if entry["repository"] != repository(registry, name):
            raise ManifestError(
                f"the manifest repository of {name} is not {repository(registry, name)}"
            )
        _text(entry["digest"], DIGEST, f"digest of {name}")
    if "tested" in manifest:
        tested = _exact_keys(manifest["tested"], TESTED_KEYS, "tested")
        _text(tested["run_id"], RUN_ID, "tested run_id")
        for suite in ("e2e", "dast"):
            if tested[suite] != "success":
                raise ManifestError(f"the manifest tested {suite} result is not success")
    return manifest


def load(path: Path, *, allow_tested: bool = True) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManifestError(f"cannot read {path}: {error.strerror}") from error
    except ValueError as error:
        raise ManifestError(f"{path} is not valid JSON") from error
    return validate(data, allow_tested=allow_tested)


def write(manifest: Mapping[str, Any], path: Path) -> None:
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def read_digests(directory: Path) -> dict[str, str]:
    """One file per image in ``directory``, named after the image and holding its digest."""
    try:
        names = sorted(item.name for item in directory.iterdir())
    except OSError as error:
        raise ManifestError(f"cannot read {directory}: {error.strerror}") from error
    if names != sorted(IMAGES):
        raise ManifestError(
            f"{directory} must hold exactly the digest files of {', '.join(IMAGES)}; found "
            f"{', '.join(names) or 'none'}"
        )
    digests = {}
    for name in IMAGES:
        digests[name] = _text(
            (directory / name).read_text(encoding="utf-8").strip(), DIGEST, f"digest of {name}"
        )
    return digests


def assemble(args: argparse.Namespace) -> int:
    digests = read_digests(args.digests)
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "version": args.version,
        "tag": f"v{args.version}",
        "commit": args.commit,
        "git_sha": args.git_sha,
        "registry": args.registry,
        "images": {
            name: {"repository": repository(args.registry, name), "digest": digests[name]}
            for name in IMAGES
        },
    }
    write(validate(manifest, allow_tested=False), args.output)
    return 0


def record(args: argparse.Namespace) -> int:
    manifest = load(args.manifest, allow_tested=False)
    for suite, result in (("e2e", args.e2e), ("dast", args.dast)):
        if result != "success":
            raise ManifestError(
                f"the {suite} result is {result!r}, not success: nothing is recorded"
            )
    manifest["tested"] = {"run_id": args.run_id, "e2e": args.e2e, "dast": args.dast}
    write(validate(manifest), args.output)
    return 0


def check(args: argparse.Namespace) -> int:
    manifest = load(args.manifest)
    if args.tested and "tested" not in manifest:
        raise ManifestError("the manifest has no tested block: E2E and DAST did not pass on it")
    for key, wanted in (
        ("version", args.version),
        ("commit", args.commit),
        ("registry", args.registry),
    ):
        if manifest[key] != wanted:
            raise ManifestError(f"the manifest {key} is {manifest[key]!r}, expected {wanted!r}")
    return 0


def entries(args: argparse.Namespace) -> int:
    manifest = load(args.manifest)
    for name in IMAGES:
        image = manifest["images"][name]
        print(f"{name}\t{image['repository']}\t{image['digest']}")
    return 0


def field(args: argparse.Namespace) -> int:
    print(load(args.manifest)[args.name])
    return 0


def pin_chart(args: argparse.Namespace) -> int:
    manifest = load(args.manifest)
    text = args.values.read_text(encoding="utf-8")
    lines = text.split("\n")
    anchors = [index for index, line in enumerate(lines) if line == ANCHOR]
    if len(anchors) != 1:
        raise ManifestError(
            f"{args.values} must hold the line {ANCHOR.strip()!r} exactly once, "
            f"found {len(anchors)}"
        )
    pinned = ["  imageDigests:"] + [
        f"    {name}: {manifest['images'][name]['digest']}" for name in IMAGES
    ]
    lines[anchors[0] : anchors[0] + 1] = pinned
    args.values.write_text("\n".join(lines), encoding="utf-8")
    return 0


def _platform_name(reference: str) -> str | None:
    """The component an image reference names, or None for an image that is not the platform's."""
    repository_part = reference.split("@", 1)[0]
    last = repository_part.rsplit("/", 1)[-1]
    if ":" in last:
        repository_part = repository_part[: len(repository_part) - len(last) + last.index(":")]
    for name in IMAGES:
        if repository_part == f"{NAMESPACE}/{name}" or repository_part.endswith(
            f"/{NAMESPACE}/{name}"
        ):
            return name
    return None


def check_rendered(args: argparse.Namespace) -> int:
    manifest = load(args.manifest)
    try:
        rendered = args.rendered.read_text(encoding="utf-8")
    except OSError as error:
        raise ManifestError(f"cannot read {args.rendered}: {error.strerror}") from error
    seen: set[str] = set()
    for line in rendered.splitlines():
        match = IMAGE_LINE.match(line)
        if match is None:
            continue
        reference = match.group("ref").strip("\"'")
        name = _platform_name(reference)
        if name is None:
            continue
        image = manifest["images"][name]
        wanted = f"{image['repository']}:{args.version}@{image['digest']}"
        if reference != wanted:
            raise ManifestError(f"the rendered {name} image is {reference}, expected {wanted}")
        seen.add(name)
    missing = [name for name in IMAGES if name not in seen]
    if missing:
        raise ManifestError(f"the rendered chart has no image for: {', '.join(missing)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, handler: Any) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.set_defaults(handler=handler)
        return sub

    sub = command("assemble", assemble)
    sub.add_argument("--digests", type=Path, required=True)
    sub.add_argument("--version", required=True)
    sub.add_argument("--commit", required=True)
    sub.add_argument("--git-sha", required=True)
    sub.add_argument("--registry", required=True)
    sub.add_argument("--output", type=Path, required=True)

    sub = command("record", record)
    sub.add_argument("--manifest", type=Path, required=True)
    sub.add_argument("--run-id", required=True)
    sub.add_argument("--e2e", required=True)
    sub.add_argument("--dast", required=True)
    sub.add_argument("--output", type=Path, required=True)

    sub = command("check", check)
    sub.add_argument("--manifest", type=Path, required=True)
    sub.add_argument("--version", required=True)
    sub.add_argument("--commit", required=True)
    sub.add_argument("--registry", required=True)
    sub.add_argument("--tested", action="store_true")

    sub = command("entries", entries)
    sub.add_argument("--manifest", type=Path, required=True)

    sub = command("field", field)
    sub.add_argument("--manifest", type=Path, required=True)
    sub.add_argument("name", choices=FIELDS)

    sub = command("pin-chart", pin_chart)
    sub.add_argument("--manifest", type=Path, required=True)
    sub.add_argument("--values", type=Path, required=True)

    sub = command("check-rendered", check_rendered)
    sub.add_argument("--manifest", type=Path, required=True)
    sub.add_argument("--version", required=True)
    sub.add_argument("--rendered", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.handler(args)
    except ManifestError as error:
        report(str(error))
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
