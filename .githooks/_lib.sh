# Shared by .githooks/pre-commit and .githooks/pre-push. Sourced, not executed.
#
# Keep BLOCKED_PATH_RE in step with the ignore lists that carry the same
# patterns: .gitignore, .dockerignore, frontend/.gitignore and the
# globalIgnores in frontend/eslint.config.js.

# Paths that must never be committed or pushed regardless of content.
BLOCKED_PATH_RE='(^|/)\.env($|\.)|\.(pem|p12|pfx|jks|key|db|sqlite3?|bak|dump)$|(^|/)(data|backups)/|id_rsa|id_ed25519|credentials\.json|service-account.*\.json|(^|/)secrets-sealed-|credentials-backup'

# stdin: one path per line. stdout: the ones that are blocked.
blocked_paths() {
  grep -Ei "$BLOCKED_PATH_RE" | grep -v '\.env\.example$' || true
}

# scan_blobs REV PATH...
# Runs tools/secret_scan.py over the content of each PATH at REV, where REV is
# "" for the index (staged content) or a commit id. Scans the git objects, not
# the working tree: those are the bytes that become the commit or the push.
# Prints the scanner report with repo-relative paths on stderr and returns the
# scanner's exit code.
scan_blobs() {
  local rev="$1"; shift
  local tmpdir dest f rc=0 out
  local to_scan=()
  tmpdir="$(mktemp -d)"
  for f in "$@"; do
    dest="$tmpdir/$f"
    mkdir -p "$(dirname "$dest")"
    if git show "${rev}:${f}" > "$dest" 2>/dev/null; then
      to_scan+=("$dest")
    fi
  done
  if [ ${#to_scan[@]} -gt 0 ]; then
    out="$(python3 tools/secret_scan.py "${to_scan[@]}" 2>&1)" || rc=$?
    if [ "$rc" -ne 0 ]; then
      # Rewrite temp paths to repo-relative ones so the report names the file
      # the author actually has to edit.
      printf '%s\n' "$out" | sed "s#${tmpdir}/##g" >&2
    fi
  fi
  rm -rf "$tmpdir"
  return "$rc"
}
