#!/usr/bin/env bash
# Render every Mermaid source in docs/diagrams/*.mmd to docs/images/diagrams/<name>.png.
# The docs embed these PNG images instead of live Mermaid blocks, so a diagram looks the same in every
# browser and on GitHub (live rendering sized boxes before the page font loaded and clipped labels).
# Uses the official Mermaid CLI image, pinned by digest. Run from the repository root after editing
# a .mmd file, and commit the source and the PNG together.
set -euo pipefail
IMAGE="minlag/mermaid-cli:12.0.1@sha256:18938e53497376d524e04b1fc0bf7b6d61a491ce9ff7032bb2855d57155acb10"
cd "$(dirname "$0")/.."
mkdir -p docs/images/diagrams
for src in docs/diagrams/*.mmd; do
  name="$(basename "$src" .mmd)"
  docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/data" "$IMAGE" \
    -i "/data/$src" -o "/data/docs/images/diagrams/$name.png" -s 2 -b white -q
  echo "rendered docs/images/diagrams/$name.png"
done
