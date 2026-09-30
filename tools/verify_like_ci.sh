#!/usr/bin/env bash
# Run the unit suite the way CI does, on CI's interpreter.
#
# v5.22.39 — v5.22.38 pushed a commit that was green locally and red on CI, twice
# over, because this machine's environment differs from a clean runner's in ways
# that change test outcomes:
#
#   - Python 3.10 here vs 3.13 on CI and in the deployed image.
#   - `http-sfv` declared but not installed here, which silently swapped the LMRH
#     parser and hid BUG-093 (a live compliance bug) for four months.
#   - An older `fastapi` here; 0.141.1 changed `app.routes`, breaking six tests.
#
# `tests/unit/test_v52239_behaviour_gating_deps.py` now catches the missing- and
# out-of-range-dependency cases locally. It cannot catch "CI resolves a newer
# version than I have", because a floor does not express that. This does: it runs
# the suite on Python 3.13 inside the project image.
#
# Not a substitute for CI, and not wired into `make test` — it needs Docker and
# takes a couple of minutes. Run it before pushing anything that touches
# dependencies, route registration, or async teardown.
#
# Usage: tools/verify_like_ci.sh [image]
set -euo pipefail

IMAGE="${1:-llm-proxy2:latest}"
STAGE="$(mktemp -d -t llmproxy-verify-ci-XXXXXX)"
# The container runs as root and leaves root-owned __pycache__ behind, so the
# cleanup needs the same privilege the run did.
cleanup() { sudo rm -rf "$STAGE" 2>/dev/null || rm -rf "$STAGE" 2>/dev/null || true; }
trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required (this runs the suite on the image's Python 3.13)" >&2
  exit 2
fi

echo "==> staging HEAD + uncommitted changes into $STAGE"
git archive HEAD | tar -x -C "$STAGE"
# Uncommitted edits, so this verifies what you are about to push.
if ! git diff --quiet; then
  git diff | (cd "$STAGE" && patch -p1 -s)
fi
# Untracked files (new tests, new modules) — without these the run is misleading.
git ls-files --others --exclude-standard | while read -r f; do
  mkdir -p "$STAGE/$(dirname "$f")"
  cp "$f" "$STAGE/$f"
done
# frontend/dist is gitignored but app/main.py only registers the SPA catch-all
# when it exists, so copy it in if built (CI builds it).
if [ -f frontend/dist/index.html ]; then
  mkdir -p "$STAGE/frontend"
  cp -r frontend/dist "$STAGE/frontend/"
fi

echo "==> running tests/unit on the image's interpreter ($IMAGE)"
# Note: three tests inspect `git ls-files` and skip/fail in this staged tree
# because it is not a git checkout. CI does a real checkout, so those are
# expected here and are reported at the end for clarity.
sudo docker run --rm -u root -v "$STAGE:/work" -w /work --entrypoint /bin/sh "$IMAGE" -c '
  python -V
  pip install --quiet --no-input pytest pytest-asyncio httpx >/dev/null 2>&1
  python -m pytest -q -p no:cacheprovider tests/unit 2>&1 | tail -25
'

cat <<'NOTE'

==> Reading the result
    This should be a clean pass. Three assertions that shell out to `git` skip
    here, because the staged tree is a copy rather than a checkout; CI does a
    real checkout so they run there. Anything else failing is a genuine
    local/CI divergence — most likely a dependency this machine resolves
    differently, which is exactly what this script exists to surface.
NOTE
