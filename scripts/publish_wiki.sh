#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="richardcmckinney"
REPO_NAME="ai-compute-intelligence"
WIKI_REMOTE="https://github.com/${REPO_OWNER}/${REPO_NAME}.wiki.git"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WIKI_SRC_DIR="${REPO_ROOT}/wiki-src"

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI is required" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "gh auth login is required before publishing wiki" >&2
  exit 1
fi

tmpdir="$(mktemp -d /tmp/aci_wiki_publish_XXXXXX)"
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

if [[ ! -d "$WIKI_SRC_DIR" ]]; then
  echo "wiki-src directory not found at ${WIKI_SRC_DIR}" >&2
  exit 1
fi

if ! git clone "$WIKI_REMOTE" "$tmpdir/wiki" >/dev/null 2>&1; then
  echo "Wiki git repository is not initialized yet." >&2
  echo "Open https://github.com/${REPO_OWNER}/${REPO_NAME}/wiki once, create any page, then rerun this script." >&2
  exit 2
fi

cp -f "$WIKI_SRC_DIR"/*.md "$tmpdir/wiki/"

cd "$tmpdir/wiki"
if git diff --quiet && git diff --cached --quiet; then
  echo "No wiki changes to publish."
  exit 0
fi

git add .
git commit -m "Update wiki content"
git push origin master

echo "Wiki published successfully."
