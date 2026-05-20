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
#  10. Backup tarball to /home/dblagbro/backups/
#  11. Print verification + per-node redeploy commands
#
# Fleet compose-image-reference inconsistency (read once and remember):
#   - tmrwww01 + tmrwww02 compose : `image: llm-proxy2:latest`        (local tag)
#   - c1conv compose              : `image: dblagbro/llm-proxy2:latest` (Hub tag)
#   The redeploy hints printed at the end of this script use the right
#   per-node form (pull+retag on tmrwww02; pull on c1conv).

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

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "ERROR: GitHub release $TAG already exists." >&2
  exit 1
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

# 4. Docker build + push (versioned tag)
run sudo docker build -t "${DOCKER_REPO}:${VERSION}" "$REPO_DIR"
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

# 6. Backup tarball
TS=$(date -u +%Y%m%dT%H%M%SZ)
TARBALL="/home/dblagbro/backups/llm-proxy-v2-${TAG}-${TS}.tar.gz"
run tar \
  --exclude='llm-proxy-v2/.git' \
  --exclude='llm-proxy-v2/__pycache__' \
  --exclude='llm-proxy-v2/.pytest_cache' \
  --exclude='llm-proxy-v2/frontend/node_modules' \
  --exclude='llm-proxy-v2/frontend/dist' \
  --exclude='*.pyc' \
  -czf "$TARBALL" -C /home/dblagbro llm-proxy-v2

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
echo "  # Node 3 — c1conv (GCP): compose uses the Hub-qualified name"
echo "  # ${DOCKER_REPO}:latest, so pull is enough — no local retag needed."
echo "  gcloud compute ssh c1conversations-avaya-01-s23 \\"
echo "    --project=feature-preview-c1convs --zone=us-central1-a \\"
echo "    --command='sudo docker pull ${DOCKER_REPO}:latest && \\"
echo "      cd /opt/C1/instance && \\"
echo "      sudo docker compose up -d --force-recreate --no-deps llm-proxy2'"
echo ""
echo "  # Verify:"
echo "  curl -s https://c1conversations-avaya-01.avaya.c1cx.com/llm-proxy2/health | python3 -m json.tool | head -6"
