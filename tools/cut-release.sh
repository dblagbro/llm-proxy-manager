#!/bin/bash
# tools/cut-release.sh — one-shot release ceremony for llm-proxy v2.
#
# Operator-locked rule: every version bump = git tag + GitHub release +
# Docker Hub push, all in the same session (see
# ~/.claude/projects/-home-dblagbro/memory/feedback_release_every_version.md).
#
# Usage:
#   tools/cut-release.sh                     # picks up version from app/__version__.py
#   tools/cut-release.sh --version 3.9.8     # explicit override
#   tools/cut-release.sh --dry-run           # print steps without executing
#
# Prerequisites:
#   - Working tree clean OR the only uncommitted change is the version bump
#   - gh CLI authenticated (gh auth status)
#   - Docker logged in to docker.io as dblagbro (docker login)
#   - Branch is v2 (or pass --target <branch>)
#
# Steps:
#   1. Parse version, validate not already released
#   2. git tag vX.Y.Z (annotated, with subject from latest commit)
#   3. git push origin v2 (if commits ahead) + git push origin vX.Y.Z
#   4. gh release create vX.Y.Z --notes-from-tag --target <branch>
#   5. sudo docker build -t dblagbro/llm-proxy2:X.Y.Z .
#   6. sudo docker push dblagbro/llm-proxy2:X.Y.Z
#   7. sudo docker tag dblagbro/llm-proxy2:X.Y.Z dblagbro/llm-proxy2:latest
#   8. sudo docker push dblagbro/llm-proxy2:latest
#   9. sudo docker tag dblagbro/llm-proxy2:X.Y.Z llm-proxy2:latest
#       — local-name retag so the on-host compose pickup is correct on
#         tmrwww01 (its compose uses the unqualified `llm-proxy2:latest`).
#         Was the v4.3.3 release-deploy footgun: without this step the
#         first `docker compose up -d --force-recreate` on tmrwww01
#         silently kept the prior container's image.
#  10. Backup tarball to /mnt/s/tmrwww01-home-backups/backups/
#  11. Print verification + per-node redeploy commands
#
# Fleet compose-image-reference inconsistency (read once and remember):
#   - tmrwww01 + tmrwww02 compose : `image: llm-proxy2:latest`        (local tag)
#   The redeploy hints printed at the end use the pull+retag form on tmrwww02.
#
# 2026-08-15 repairs (this script had not cut a release since v5.21.16, and
# 12 versions shipped untagged/unpublished behind it — these were why):
#   1. Pre-cut live-verify curled a THIRD canonical URL, the GCP node
#      c1conversations-avaya-01.avaya.c1cx.com. That node is dropped and
#      off-limits, so the check failed and `exit 1`-ed EVERY cut. Removed —
#      the two TMR nodes are the whole cluster now. (Not worked around with
#      --skip-live-verify, which would also disable the checks we want.)
#   2. Backup tarball tar'd `-C /home/dblagbro llm-proxy-v2` — the pre-move
#      copy. Source of truth moved to /mnt/s/code/llm-proxy-v2 on 2026-08-13.
#   3. Docker build read its context from the NFS source dir (~305M over
#      NFS). Now stages to local disk via stage-build.sh first, matching the
#      documented build path.
#   4. Closing hints printed `gcloud compute ssh` redeploy commands for the
#      GCP node. Removed — GCP is out of scope until the operator says so.
#   5. Added a README version-drift guard (the operator-locked rule's step 1;
#      README had rotted to v4.4.18 while v5.22.11 shipped).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=""
BRANCH="v2"
DRY_RUN=0
DOCKER_REPO="dblagbro/llm-proxy2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --target)  BRANCH="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$VERSION" ]]; then
  VERSION=$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('$REPO_DIR/app/__version__.py').read()).group(1))")
fi
TAG="v${VERSION}"

run() {
  echo "+ $*"
  # NB: an `&&` short-circuit here returns 1 when DRY_RUN=1, which trips
  # `set -e` and aborts the dry-run after the first step. Use an explicit
  # `if` so a skipped step returns 0.
  if [[ $DRY_RUN -eq 0 ]]; then
    "$@"
  fi
}

echo "=== llm-proxy v2 release ceremony ==="
echo "Repo:    $REPO_DIR"
echo "Version: $VERSION (tag: $TAG)"
echo "Branch:  $BRANCH"
echo "Dry-run: $DRY_RUN"
echo ""

cd "$REPO_DIR"

# P4 enhancement: bug-log.md sync check. If commits mention BUG-### but
# bug-log.md wasn't updated, warn the operator so they can manually verify
# the bug status was tracked. Non-fatal to preserve release-emergency
# flexibility.
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
if [[ -n "$LAST_TAG" ]]; then
  echo "=== Bug-log sync check (since $LAST_TAG) ==="

  # Extract all BUG-### references from commit messages since last tag
  BUG_REFS=$(git log "${LAST_TAG}..HEAD" --format='%s %b' | grep -oE 'BUG-[0-9]+' | sort -u || true)

  if [[ -n "$BUG_REFS" ]]; then
    echo "Found bug references in commits: $(echo $BUG_REFS | tr '\n' ' ')"

    # Check if bug-log.md was modified in any commit since last tag
    BUGLOG_UPDATED=$(git log "${LAST_TAG}..HEAD" --oneline --name-only | grep -q '^bug-log\.md$' && echo "yes" || echo "no")

    if [[ "$BUGLOG_UPDATED" == "no" ]]; then
      echo "⚠️  WARNING: Commits reference bugs but bug-log.md was not updated" >&2
      echo "   Bug references found: $(echo $BUG_REFS | tr '\n' ' ')" >&2
      echo "   Verify bug statuses are tracked before releasing." >&2
      echo "" >&2

      # Give operator 5 seconds to ctrl-C if this is wrong
      if [[ $DRY_RUN -eq 0 ]]; then
        echo "Continuing in 5 seconds... (ctrl-C to abort)" >&2
        sleep 5
      fi
    else
      echo "✓ bug-log.md was updated (references validated)"
    fi
  else
    echo "No bug references found in commits"
  fi
  echo ""
fi

# Sanity: working tree (allow staged version bump; reject unstaged changes
# in tracked files other than .pyc bytecode)
if ! git diff --quiet -- ':(exclude)*.pyc'; then
  echo "ERROR: unstaged changes present. Commit/stash before releasing." >&2
  git diff --stat -- ':(exclude)*.pyc' >&2
  exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "ERROR: tag $TAG already exists locally. Pick a new version." >&2
  exit 1
fi

# README version-drift guard. Step 1 of the operator-locked release rule is
# "README bump" — and it is the step that rots silently, because nothing here
# checked it: on 2026-08-15 the README still read v4.4.18 while v5.22.11 was
# live. Fatal on purpose; the fix is one line.
README_VER=$(grep -oE 'Current version: \*\*v[0-9]+\.[0-9]+\.[0-9]+\*\*' "$REPO_DIR/README.md" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || echo "")
if [[ -z "$README_VER" ]]; then
  echo "ERROR: could not find the 'Current version: **vX.Y.Z**' line in README.md." >&2
  echo "  The release rule requires the README to advertise the shipping version." >&2
  exit 1
fi
if [[ "$README_VER" != "$VERSION" ]]; then
  echo "ERROR: README.md says v$README_VER but this release is v$VERSION." >&2
  echo "  Fix: update the 'Current version:' line in README.md, commit, re-run." >&2
  exit 1
fi
echo "✓ README version matches ($README_VER)"

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "ERROR: GitHub release $TAG already exists." >&2
  exit 1
fi

# 0. Pre-cut live-verify (L3 / Batch E — added 2026-05-20).
# Hits the 3 canonical health URLs before tagging. The point is to
# catch a fleet that's silently broken at deploy time — if any node
# is unreachable / unhealthy / on the wrong version, tagging a new
# release would compound the problem (downstream consumers chase
# the new image while the current one is already misbehaving).
# Aborts the cut if any check fails; ``--skip-live-verify`` flag
# is the explicit bypass for legitimate cases (e.g. cutting a
# release whose deploy ITSELF fixes the broken state).
SKIP_LIVE_VERIFY="${SKIP_LIVE_VERIFY:-0}"
for arg in "$@"; do
  if [ "$arg" = "--skip-live-verify" ]; then
    SKIP_LIVE_VERIFY=1
  fi
done

if [ "$SKIP_LIVE_VERIFY" != "1" ]; then
  echo "=== Pre-cut live-verify ==="
  # The cluster is exactly these two nodes. Do NOT re-add a GCP URL here:
  # doing so aborted every release cut from v5.21.16 (2026-08-06) onward.
  CANONICAL_URLS=(
    "https://www.voipguru.org/llm-proxy2/health"
    "https://www2.voipguru.org/llm-proxy2/health"
  )
  VERIFY_FAILED=0
  for url in "${CANONICAL_URLS[@]}"; do
    # Allow up to 10s per node. Look for `"status":"healthy"` AND
    # `"version":"..."` (any value — we don't pin to current since
    # the new tag is about to ship the new version).
    resp=$(curl -sf --max-time 10 "$url" 2>&1) || {
      echo "  $url: FAIL (curl exit $?)"
      VERIFY_FAILED=1
      continue
    }
    status=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','MISSING'))" 2>/dev/null) || status="PARSE_FAIL"
    version=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','MISSING'))" 2>/dev/null) || version="PARSE_FAIL"
    if [ "$status" != "healthy" ]; then
      echo "  $url: FAIL (status=$status)"
      VERIFY_FAILED=1
    else
      echo "  $url: OK (status=$status version=$version)"
    fi
  done
  if [ "$VERIFY_FAILED" = "1" ]; then
    echo ""
    echo "ERROR: pre-cut live-verify failed on one or more canonical URLs." >&2
    echo "  Fix the unhealthy node(s) before cutting $TAG, OR re-run with --skip-live-verify" >&2
    echo "  if the new release ITSELF is the fix for the broken state." >&2
    exit 1
  fi
  echo "All canonical URLs healthy — proceeding with cut."
  echo ""
fi

# 1. Tag
SUBJECT=$(git log -1 --format='%s')
BODY=$(git log -1 --format='%b')
run git tag -a "$TAG" -m "$SUBJECT"

# 2. Push branch + tag
run git push origin "$BRANCH"
run git push origin "$TAG"

# 3. GitHub release (notes = commit body; falls back to subject if empty)
NOTES="${BODY:-See [CHANGELOG.md](CHANGELOG.md) entry for $TAG.}"
run gh release create "$TAG" --title "$TAG — ${SUBJECT#*: }" --notes "$NOTES" --target "$BRANCH"

# 4. Docker build + push (versioned tag).
#    Build from the LOCAL staging tree, not $REPO_DIR: the source of truth is
#    on the /mnt/s NFS share and docker reads the entire ~305M context on
#    every build. stage-build.sh rsyncs SAN -> local disk; building the SAN
#    path directly is slow and puts a hard NFS mount in the build's critical
#    path. Staging is a no-op refresh when nothing changed.
STAGE_SCRIPT=/home/dblagbro/docker/scripts/stage-build.sh
BUILD_CTX=/home/dblagbro/docker/build/llm-proxy-v2
if [[ -x "$STAGE_SCRIPT" ]]; then
  run "$STAGE_SCRIPT" llm-proxy-v2
else
  echo "WARNING: $STAGE_SCRIPT not found — building from $REPO_DIR (slow, NFS)." >&2
  BUILD_CTX="$REPO_DIR"
fi
run sudo docker build -t "${DOCKER_REPO}:${VERSION}" "$BUILD_CTX"
run sudo docker push "${DOCKER_REPO}:${VERSION}"

# 5. Retag + push :latest (Hub-qualified)
run sudo docker tag "${DOCKER_REPO}:${VERSION}" "${DOCKER_REPO}:latest"
run sudo docker push "${DOCKER_REPO}:latest"

# 5b. Local-name retag for the on-host compose pickup.
#     Without this step, tmrwww01's `docker compose up -d
#     --force-recreate llm-proxy2` keeps the OLD container's image —
#     because tmrwww01's compose references `image: llm-proxy2:latest`
#     (unqualified) and that local tag wasn't updated by the Hub push
#     above. Silently re-runs the previous version. (Was the v4.3.3
#     release-deploy footgun on 2026-05-19.)
run sudo docker tag "${DOCKER_REPO}:${VERSION}" "llm-proxy2:latest"

# 6. Backup tarball — write directly to NFS to avoid local-disk fill-up.
TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/mnt/s/tmrwww01-home-backups/backups"
mkdir -p "$BACKUP_DIR"
TARBALL="$BACKUP_DIR/llm-proxy-v2-${TAG}-${TS}.tar.gz"
# Tar the SOURCE OF TRUTH (/mnt/s/code), not /home/dblagbro — the latter is
# the pre-move copy as of 2026-08-13 and would archive stale source.
run tar \
  --exclude='llm-proxy-v2/.git' \
  --exclude='llm-proxy-v2/__pycache__' \
  --exclude='llm-proxy-v2/.pytest_cache' \
  --exclude='llm-proxy-v2/.ruff_cache' \
  --exclude='llm-proxy-v2/frontend/node_modules' \
  --exclude='llm-proxy-v2/frontend/dist' \
  --exclude='*.pyc' \
  -czf "$TARBALL" -C /mnt/s/code llm-proxy-v2

echo ""
echo "=== Done. Verification commands: ==="
echo "  gh release view $TAG"
echo "  curl -s https://hub.docker.com/v2/repositories/${DOCKER_REPO}/tags?page_size=5 | python3 -m json.tool | grep -A1 \\\"name\\\""
echo "  ls -lh $TARBALL"
echo ""
echo "To redeploy on fleet (rolling, one node at a time):"
echo ""
echo "  # Node 1 — tmrwww01 (this host): local llm-proxy2:latest tag was"
echo "  # already updated in step 5b above. Plain compose recreate is enough."
echo "  sudo docker compose --project-directory /home/dblagbro/docker \\"
echo "    up -d --force-recreate --no-deps llm-proxy2"
echo ""
echo "  # Verify before proceeding:"
echo "  curl -s https://www.voipguru.org/llm-proxy2/health | python3 -m json.tool | head -6"
echo ""
echo "  # Node 2 — tmrwww02: same compose pattern as tmrwww01 (local-tag"
echo "  # reference), so the recreate must be preceded by a pull + local retag."
echo "  ssh tmrwww02 \"sudo docker pull ${DOCKER_REPO}:${VERSION} && \\"
echo "    sudo docker tag ${DOCKER_REPO}:${VERSION} llm-proxy2:latest && \\"
echo "    sudo docker compose --project-directory /home/dblagbro/docker \\"
echo "    up -d --force-recreate --no-deps llm-proxy2\""
echo ""
echo "  # Verify:"
echo "  curl -s https://www2.voipguru.org/llm-proxy2/health | python3 -m json.tool | head -6"
echo ""
echo "  # That is the whole fleet — two nodes. GCP is out of scope."
