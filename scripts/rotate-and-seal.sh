#!/usr/bin/env bash
# Project wrapper for the Secret-Handling Protocol (docs/SECRET-HANDLING-PROTOCOL.md).
#
# Thin configuration layer over the operator's portable rotate-and-seal.sh:
# it sets this project's secret store, restart command and the NAMES of the
# variables that may be rotated, then hands off. It holds no secret values and
# prints none; the portable script prints fingerprints only.
#
# Nothing runs unless you invoke it with at least one variable name. It is not
# called by the Makefile, the test suite, CI or any hook.
#
# Usage (operator, on the node being rotated):
#   GPG_RECIPIENT="<your key id or email>" scripts/rotate-and-seal.sh LLM_PROXY2_SECRET_KEY
#
# Read before rotating:
#   * LLM_PROXY2_SECRET_KEY is passed to the container as SECRET_KEY. Besides
#     session signing, app/auth/key_encryption.py derives the Fernet key for
#     stored API keys from it, and the app has no versioned-key support. After a
#     rotation, rows encrypted under the old value no longer decrypt. The old
#     value is retained in ENV_FILE as LLM_PROXY2_SECRET_KEY_PREV (RETAIN_AS)
#     and in the sealed backup so a re-encryption or rollback is possible.
#   * ENV_FILE is per node. tmrwww01 and tmrwww02 each have their own; a
#     rotation applied to one node only leaves the cluster with two keys.
#   * RECREATE_CMD recreates the llm-proxy2 container only (--no-deps). Secrets
#     consumed by other containers (for example the grok bridge token) are
#     deliberately not in the list below, because this command would not
#     restart their consumer.
set -euo pipefail

PROTOCOL_SCRIPT="${PROTOCOL_SCRIPT:-$HOME/.claude/protocols/secret-handling/rotate-and-seal.sh}"

# Variable NAMES in ENV_FILE that this wrapper may rotate. Names only.
ROTATABLE="LLM_PROXY2_SECRET_KEY"

usage() {
  awk 'NR>1 && !/^#/ {exit} NR>1 {sub(/^# ?/, ""); print}' "$0"
  echo
  echo "Rotatable variable names: $ROTATABLE"
}

if [ "$#" -eq 0 ]; then
  usage >&2
  exit 2
fi

for name in "$@"; do
  case " $ROTATABLE " in
    *" $name "*) ;;
    *) echo "refusing: '$name' is not in the rotatable list ($ROTATABLE)" >&2; exit 2 ;;
  esac
done

if [ ! -f "$PROTOCOL_SCRIPT" ]; then
  echo "portable script not found: $PROTOCOL_SCRIPT" >&2
  exit 1
fi
: "${GPG_RECIPIENT:?set GPG_RECIPIENT (your GPG key id/email); see docs/SECRET-HANDLING-PROTOCOL.md}"

export GPG_RECIPIENT
export ENV_FILE="${ENV_FILE:-/home/dblagbro/docker/.env}"
export SECRETS="$*"
export RETAIN_AS="${RETAIN_AS:-LLM_PROXY2_SECRET_KEY:LLM_PROXY2_SECRET_KEY_PREV}"
export RECREATE_CMD="${RECREATE_CMD:-cd /home/dblagbro/docker && sudo docker compose up -d --force-recreate --no-deps llm-proxy2}"

exec bash "$PROTOCOL_SCRIPT"
