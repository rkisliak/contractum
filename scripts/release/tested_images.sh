#!/usr/bin/env bash
# Operations on the exact images of a release, by digest: `bash scripts/release/tested_images.sh
# <command> [tag]`. The release workflow (release.yml) and the test runs it calls use it, so the
# images that E2E and DAST test are the images that are promoted.
#
# DC_RELEASE_IMAGES names the manifest (release-images.json, later tested-images.json, written by
# scripts/release/release_images.py). The script refuses to run when it is unset or the file is
# missing. Only digests are trusted: a tag is read back and compared, never followed.
#
#   load                 pull every image by digest and tag it contractum/<name>:dev, the name
#                        compose.yaml runs, so the stack starts on those images with --no-build.
#                        Refuses unless HEAD is the commit the images were built from
#   verify-tags <tag>    every <repository>:<tag> resolves to the digest of the manifest
#   verify-attestations  gh attestation verify of the manifest file and of every image digest:
#                        signed by this repository's release workflow, built from the manifest
#                        commit on main, on a GitHub-hosted runner
#   verify-stack         the running compose stack uses exactly these images and reports the
#                        manifest version (needs .dev/ca.crt and the gateway on :8443)
#   tag <new-tag>        add <new-tag> to every image digest without a rebuild (imagetools
#                        create), then verify-tags <new-tag>
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

die() {
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    printf '::error::%s\n' "$*" >&2
  else
    printf 'error: %s\n' "$*" >&2
  fi
  exit 1
}

manifest="${DC_RELEASE_IMAGES:-}"
[ -n "$manifest" ] || die "DC_RELEASE_IMAGES is not set: it names the release image manifest"
[ -f "$manifest" ] || die "DC_RELEASE_IMAGES points at ${manifest}, which is not a file"

images() { python3 scripts/release/release_images.py entries --manifest "$manifest"; }
field() { python3 scripts/release/release_images.py field --manifest "$manifest" "$1"; }

# Each command reads the manifest once, so a malformed file stops it before any docker call.
entries="$(images)"
version="$(field version)"
commit="$(field commit)"
git_sha="$(field git_sha)"

command="${1:-}"
tag_arg="${2:-}"

# for_each_image CALLBACK: CALLBACK name repository digest, for every image in manifest order.
for_each_image() {
  local callback="$1" name repository digest
  while IFS=$'\t' read -r name repository digest; do
    "$callback" "$name" "$repository" "$digest"
  done <<<"$entries"
}

load_one() {
  local name="$1" repository="$2" digest="$3" ref="$2@$3"
  docker pull "$ref" >/dev/null
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$ref" \
    | grep -Fxq "$ref" || die "${name}: the pulled image does not hold the reference ${ref}"
  docker tag "$ref" "contractum/${name}:dev"
  printf 'loaded %s %s\n' "$name" "$digest"
}

verify_tag_one() {
  local name="$1" repository="$2" digest="$3" seen
  seen="$(docker buildx imagetools inspect "${repository}:${VERIFY_TAG}" --format '{{json .Manifest.Digest}}' | tr -d '"[:space:]')"
  [ "$seen" = "$digest" ] \
    || die "${name}: ${repository}:${VERIFY_TAG} is ${seen:-unreadable}, the manifest digest is ${digest}"
  printf 'tag %s %s is %s\n' "$name" "$VERIFY_TAG" "$digest"
}

attestation_flags() {
  : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
  ATTEST_FLAGS=(
    --repo "$GITHUB_REPOSITORY"
    --signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release.yml"
    --source-digest "$commit"
    --source-ref refs/heads/main
    --deny-self-hosted-runners
  )
}

attest_one() {
  local name="$1" repository="$2" digest="$3"
  gh attestation verify "oci://${repository}@${digest}" "${ATTEST_FLAGS[@]}" >/dev/null \
    || die "${name}: the attestation of ${repository}@${digest} did not verify"
  printf 'attested %s %s\n' "$name" "$digest"
}

tag_one() {
  local name="$1" repository="$2" digest="$3"
  docker buildx imagetools create --tag "${repository}:${NEW_TAG}" "${repository}@${digest}" >/dev/null
  printf 'tagged %s %s\n' "$name" "${NEW_TAG}"
}

# The expected image id of each component is kept in expected_<name> (dashes become underscores),
# so the script also runs under the bash 3.2 of macOS.
stack_one() {
  local name="$1" repository="$2" digest="$3" expected
  expected="$(docker image inspect --format '{{.Id}}' "${repository}@${digest}")" \
    || die "${name}: ${repository}@${digest} is not loaded; run load first"
  printf -v "expected_${name//-/_}" '%s' "$expected"
}

json_get() {
  python3 -I -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

verify_stack() {
  for_each_image stack_one
  local ids container config image image_id name want_var seen_names=" " ui_id="" mail_id=""
  ids="$(docker ps -a -q --filter label=com.docker.compose.project=contractum)"
  for container in $ids; do
    read -r image image_id < <(docker inspect --format '{{.Config.Image}} {{.Image}}' "$container")
    case "$image" in
      contractum/e2e:dev | contractum/ui-e2e:dev) continue ;;
      contractum/*) ;;
      *) continue ;;
    esac
    name="${image#contractum/}"
    name="${name%%:*}"
    want_var="expected_${name//-/_}"
    [ -n "${!want_var:-}" ] || die "container ${container} runs ${image}, which is not a release image"
    [ "$image" = "contractum/${name}:dev" ] || die "container ${container} runs ${image}, expected contractum/${name}:dev"
    [ "$image_id" = "${!want_var}" ] \
      || die "${name}: container ${container} runs ${image_id}, the release image is ${!want_var}"
    seen_names="${seen_names}${name} "
    [ "$name" != "ui" ] || ui_id="$container"
    [ "$name" != "mail" ] || mail_id="$container"
  done
  while IFS=$'\t' read -r name _ _; do
    case "$seen_names" in *" ${name} "*) ;; *) die "${name}: no container of the stack runs the release image" ;; esac
  done <<<"$entries"

  local module body got
  for module in identity contracts validation audit docs; do
    body="$(curl -fsS --cacert .dev/ca.crt "https://localhost:8443/api/v1/${module}/version")" \
      || die "${module}: the version route did not answer"
    got="$(printf '%s' "$body" | json_get version)"
    [ "$got" = "$version" ] || die "${module}: reports version ${got}, the release is ${version}"
    got="$(printf '%s' "$body" | json_get commit)"
    [ "$got" = "$git_sha" ] || die "${module}: reports commit ${got}, the release is ${git_sha}"
  done
  config="$(docker exec "$ui_id" cat /tmp/ui/config.json)" || die "ui: config.json is not readable"
  got="$(printf '%s' "$config" | json_get version)"
  [ "$got" = "$version" ] || die "ui: config.json says version ${got}, the release is ${version}"
  got="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$mail_id" | sed -n 's/^DC_VERSION=//p')"
  [ "$got" = "$version" ] || die "mail: DC_VERSION is ${got:-unset}, the release is ${version}"
  printf 'stack runs the release %s (%s)\n' "$version" "$git_sha"
}

case "$command" in
  load)
    head="$(git rev-parse HEAD)"
    [ "$head" = "$commit" ] || die "HEAD is ${head}, the images were built from ${commit}"
    for_each_image load_one
    ;;
  verify-tags)
    [ -n "$tag_arg" ] || die "verify-tags needs a tag"
    VERIFY_TAG="$tag_arg"
    for_each_image verify_tag_one
    ;;
  verify-attestations)
    attestation_flags
    gh attestation verify "$manifest" "${ATTEST_FLAGS[@]}" >/dev/null \
      || die "the attestation of ${manifest} did not verify"
    printf 'attested manifest %s\n' "$manifest"
    for_each_image attest_one
    ;;
  verify-stack)
    verify_stack
    ;;
  tag)
    [ -n "$tag_arg" ] || die "tag needs the new tag"
    NEW_TAG="$tag_arg"
    VERIFY_TAG="$tag_arg"
    for_each_image tag_one
    for_each_image verify_tag_one
    ;;
  *)
    die "unknown command '${command}' (load, verify-tags, verify-attestations, verify-stack or tag)"
    ;;
esac
