#!/bin/bash
# tools/cut-release.sh — one-shot release ceremony for llm-proxy v2.
#
# Requires explicit destination, tested image, backup directory, and the health
# endpoints for every running gateway. A clean committed tree is required.
# The image is built and matched against the tested ID before publication.
# Example: --docker-repo namespace/repository --tested-image-id sha256:...
#          --backup-dir /secure/backups --health-url https://host/health

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=""
BRANCH="main"
DRY_RUN=0
DOCKER_REPO=""
TESTED_IMAGE_ID=""
BACKUP_DIR=""
HEALTH_URLS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --target)  BRANCH="$2"; shift 2 ;;
    --docker-repo) DOCKER_REPO="$2"; shift 2 ;;
    --tested-image-id) TESTED_IMAGE_ID="$2"; shift 2 ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --health-url) HEALTH_URLS+=("$2"); shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ ! "$DOCKER_REPO" =~ ^[a-z0-9]+([._-][a-z0-9]+)*/[a-z0-9]+([._-][a-z0-9]+)*$ ]]; then
  echo "ERROR: specify the reviewed Docker Hub destination with --docker-repo namespace/repository." >&2
  exit 2
fi

if [[ ! "$TESTED_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "ERROR: specify the exact tested image ID using --tested-image-id sha256:..." >&2
  exit 2
fi

# Fail closed if the operator omits a backup location or health endpoints.
if [[ "$BACKUP_DIR" != /* || "$BACKUP_DIR" == / || "$BACKUP_DIR" == *"/../"* || "$BACKUP_DIR" == */.. || -L "$BACKUP_DIR" || ! -d "$BACKUP_DIR" || ! -w "$BACKUP_DIR" ]]; then
  echo "ERROR: --backup-dir must be an existing, writable absolute directory without traversal or a final symlink." >&2
  exit 2
fi
if (( ${#HEALTH_URLS[@]} == 0 )); then
  echo "ERROR: provide --health-url for each gateway." >&2
  exit 2
fi
if ! python3 - "${HEALTH_URLS[@]}" <<'PYURL'
import sys
from urllib.parse import urlsplit

for url in sys.argv[1:]:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        sys.exit(1)
PYURL
then
  echo "ERROR: health URLs must be HTTPS with a host, without credentials, query, or fragment." >&2
  exit 2
fi

SOURCE_VERSION=$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('$REPO_DIR/app/__version__.py').read()).group(1))")
if [[ -z "$VERSION" ]]; then
  VERSION="$SOURCE_VERSION"
elif [[ "$VERSION" != "$SOURCE_VERSION" ]]; then
  echo "ERROR: requested version differs from app/__version__.py ($SOURCE_VERSION)." >&2
  exit 1
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

# A release must describe exactly one committed source tree. Staged changes and
# untracked files cannot be represented by the release commit either.
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "ERROR: release requires a clean working tree, including untracked files." >&2
  git status --short >&2
  exit 1
fi
if [[ "$(git branch --show-current)" != "$BRANCH" ]]; then
  echo "ERROR: checked-out branch does not match release target $BRANCH." >&2
  exit 1
fi
RELEASE_COMMIT=$(git rev-parse --verify HEAD)
echo "Release commit: $RELEASE_COMMIT"

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "ERROR: tag $TAG already exists locally. Pick a new version." >&2
  exit 1
fi
if ! REMOTE_TAG=$(GIT_TERMINAL_PROMPT=0 git ls-remote --tags origin "refs/tags/$TAG"); then
  echo "ERROR: could not check remote tags." >&2
  exit 1
fi
if [[ -n "$REMOTE_TAG" ]]; then
  echo "ERROR: tag $TAG already exists remotely." >&2
  exit 1
fi

# Require the documented version to match the release version.
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

# A public registry lookup must succeed before absence of the version tag can
# be trusted. Do not overwrite a versioned tag during an ordinary release.
REPO_STATUS=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "https://hub.docker.com/v2/repositories/$DOCKER_REPO/")
if [[ "$REPO_STATUS" != 200 ]]; then
  echo "ERROR: Docker Hub repository lookup failed; status $REPO_STATUS." >&2
  exit 1
fi
TAG_STATUS=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "https://hub.docker.com/v2/repositories/$DOCKER_REPO/tags/$VERSION/")
if [[ "$TAG_STATUS" != 404 ]]; then
  echo "ERROR: Docker Hub version tag exists or lookup failed; status $TAG_STATUS." >&2
  exit 1
fi

# Authentication failures and API errors are not evidence of an absent release.
gh auth status >/dev/null
RELEASE_REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
if RELEASE_LOOKUP=$(gh api "repos/$RELEASE_REPO/releases/tags/$TAG" 2>&1); then
  echo "ERROR: GitHub release $TAG already exists." >&2
  exit 1
elif [[ "$RELEASE_LOOKUP" != *"HTTP 404"* ]]; then
  echo "ERROR: could not verify whether GitHub release $TAG exists." >&2
  exit 1
fi

# Every explicitly specified health endpoint must be reachable and healthy.
echo "=== Pre-cut live-verify ==="
VERIFY_FAILED=0
for url in "${HEALTH_URLS[@]}"; do
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
  echo "ERROR: pre-cut live verification failed." >&2
  exit 1
fi
echo "All configured endpoints healthy."

# Build an archive of the exact commit, not an independently refreshed staging
# directory. Build before publishing so build failure cannot create a release.
if [[ $DRY_RUN -eq 0 ]]; then
  BUILD_CTX=$(mktemp -d "${TMPDIR:-/tmp}/llm-proxy-release.XXXXXXXX")
  trap 'rm -rf -- "$BUILD_CTX"' EXIT
  # The staging directory is private. Keep tracked modes independent of the operator umask.
  git archive --format=tar "$RELEASE_COMMIT" | tar -xpf - -C "$BUILD_CTX"
else
  BUILD_CTX="<temporary archive of $RELEASE_COMMIT>"
fi
run sudo docker build -t "${DOCKER_REPO}:${VERSION}" "$BUILD_CTX"
if [[ $DRY_RUN -eq 0 ]]; then
  BUILT_IMAGE_ID=$(sudo docker image inspect --format '{{.Id}}' "${DOCKER_REPO}:${VERSION}")
  if [[ "$BUILT_IMAGE_ID" != "$TESTED_IMAGE_ID" ]]; then
    echo "ERROR: rebuilt image differs from the tested image; no release was published." >&2
    exit 1
  fi
fi

# Archive and verify the exact source before publishing anything.
TS=$(date -u +%Y%m%dT%H%M%SZ)
TARBALL="$BACKUP_DIR/llm-proxy-v2-${TAG}-${TS}.tar.gz"
if [[ $DRY_RUN -eq 0 ]]; then
  if ! (set -o noclobber; : > "$TARBALL"); then
    echo "ERROR: source backup already exists or cannot be created." >&2
    exit 1
  fi
fi
run git archive --format=tar.gz --prefix="llm-proxy-v2/" --output="$TARBALL" "$RELEASE_COMMIT"
if [[ $DRY_RUN -eq 0 ]]; then
  tar -tzf "$TARBALL" >/dev/null
  sha256sum "$TARBALL"
fi

# Tag and publish only after image build and backup succeed. Publication is
# multi-step; if a remote step fails, inspect its state before retrying.
SUBJECT=$(git log -1 --format='%s')
BODY=$(git log -1 --format='%b')
run git tag -a "$TAG" -m "$SUBJECT" "$RELEASE_COMMIT"
run git push origin "$RELEASE_COMMIT:refs/heads/$BRANCH"
run git push origin "$TAG"
NOTES="${BODY:-See [CHANGELOG.md](CHANGELOG.md) entry for $TAG.}"
run gh release create "$TAG" --title "$TAG — ${SUBJECT#*: }" --notes "$NOTES" --target "$BRANCH"
run sudo docker push "${DOCKER_REPO}:${VERSION}"
# Promotion to :latest is a separate step after the registry pull test.

echo ""
echo "=== Done. Verification commands: ==="
echo "  gh release view $TAG"
echo "  curl -s https://hub.docker.com/v2/repositories/${DOCKER_REPO}/tags?page_size=5 | python3 -m json.tool | grep -A1 \\\"name\\\""
echo "  ls -lh $TARBALL"
echo ""
echo "Before deployment: record the registry digest and follow the reviewed"
echo "per-node migration and rollback plan in docs/service-uid-migration.md."
echo "Neither :latest nor the local Compose image tag was changed."
echo "Promote :latest only after the versioned registry image passes its pull test."
