#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
image=${OPEN_WEBUI_IMAGE:-ghcr.io/open-webui/open-webui:v0.11.0-slim}
container=$(docker create "$image")
temporary=$(mktemp -d "$script_dir/.dist.XXXXXX")

cleanup() {
  docker rm "$container" >/dev/null 2>&1 || true
  rm -rf "$temporary"
}
trap cleanup EXIT INT TERM

docker cp "$container:/app/build/." "$temporary/"
rm -rf "$script_dir/dist"
mv "$temporary" "$script_dir/dist"
trap - EXIT INT TERM
docker rm "$container" >/dev/null

test -f "$script_dir/dist/index.html"
echo "Extracted the Open WebUI v0.11.0 frontend into web/dist."
