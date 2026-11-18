# Secret-Handling Protocol (portable, all projects)

**Purpose:** let an AI coding agent generate, apply, rotate, and back up secrets (encryption keys,
session peppers, API tokens, DB passwords) **without any plaintext secret ever appearing in a chat
transcript, the agent's output, a log, or git** — while leaving an encrypted backup that **only you**
can open.

## The model: GPG asymmetric sealing

- **You** hold a GPG **private key**. It is the one secret you guard; it is **never shared with the
  agent, ever** — the protocol is built so it never needs to be. Store it in a password manager or on
  a hardware token.
- **The agent** may use your GPG **public key** (not secret) to *seal* (encrypt) secrets, orchestrate
  rotation, and verify by **SHA-256 fingerprint**. The agent must **never** print, commit, or log a
  plaintext secret or your private key. Only fingerprints go in chat.

Because sealing uses only the public key, the agent can produce an encrypted backup at any time with
**no secret input from you**. You "grant access" only by decrypting a sealed file yourself.

## One-time setup (you)

    gpg --quick-generate-key "<Project> Secrets <you@example.com>" rsa4096 cert,encr never
    gpg --armor --export "<Project> Secrets" > ~/.config/<project>-secrets.pub.asc
    gpg --armor --export-secret-keys "<Project> Secrets"   # store THIS output where only you can (offline / password manager)

Give the agent only the recipient name (e.g. "<Project> Secrets"). Never the private key or passphrase.

## Routine rotation (the agent runs `rotate-and-seal.sh`, or you do)

1. New secrets are generated on the host (`openssl rand`), held only in memory.
2. Applied to the target secret store (e.g. a `0600` `.env`), keeping the prior key retained for
   decryption where the system supports versioned keys (so nothing breaks mid-rotation).
3. An encrypted backup `secrets-sealed-<timestamp>.asc` is written to a `0700` vault **outside** the
   repo, encrypted to your GPG public key.
4. Only **fingerprints** are printed.
5. The service is restarted/recreated; any key-migration step runs (optional project hook).

## Recovery (you)

    gpg --decrypt <vault>/secrets-sealed-<timestamp>.asc

## Exposure protocol — "if the unlock key is ever shared, change it and everything it unlocked"

If your GPG private key or passphrase is ever exposed or suspected:
1. Generate a **new** keypair.
2. **Rotate every secret** sealed under the old key (re-run `rotate-and-seal.sh` → fresh secrets,
   sealed to the new public key).
3. Revoke/retire the old GPG key and the old sealed backups.

A compromised unlock key therefore never yields anything still in use.

## Invariants (never violated)

- No plaintext secret in chat, agent output, commit, PR, or log — fingerprints only.
- The agent never receives the private key or any passphrase.
- Sealed backups and `.env` files are never committed — enforce `secrets-sealed-*`, `.env`,
  `*credentials-backup*`, keys, and db dumps in **every** ignore list a project uses (gitignore,
  dockerignore, prettierignore, eslint ignore, pre-push hook).

## Lifecycle tool: `secrets-keyctl.sh`

One command for the whole lifecycle (you run it; your passphrase goes to GPG, never to the agent):

    ./secrets-keyctl.sh init          # create keypair + export public key
    ./secrets-keyctl.sh backup-key    # export PRIVATE key to a 0600 file -> move offline
    ./secrets-keyctl.sh status        # key fingerprint, pubkey path, sealed backups
    ./secrets-keyctl.sh rotate        # mint + apply + seal new secrets (wraps rotate-and-seal.sh)
    ./secrets-keyctl.sh reinit        # exposure protocol: new keypair (then run rotate)
    ./secrets-keyctl.sh reset         # remove local key material (double-confirm)

## Granting the agent transient access (rare)

The protocol is built so the agent almost never needs a plaintext secret — values flow host->file,
not through chat. For the rare case where a command genuinely needs an existing secret:

    ./secrets-keyctl.sh grant <VAR> [ttl]     # you run this; GPG prompts your passphrase

`grant` decrypts just that one value into a `0600` tmpfs file (`$GRANT_DIR/<VAR>`) that auto-shreds
after `ttl` seconds (default 300). It prints only a fingerprint + the path. The agent then uses it
**by reference** (`export VAR="$(cat $GRANT_DIR/VAR)"`) and never prints it. `revoke` shreds all
grants immediately. Your GPG passphrase, entered to GPG locally, is the "one-time approval" — it
never reaches the agent. (A push-to-phone / tap-to-approve gate can be layered on top later; it needs
a small approval service and is not required for the above to work.)

## Project wrapper (llm-proxy-v2)

The text above is the portable protocol, copied unchanged from
`~/.claude/protocols/secret-handling/PROTOCOL.md` on 2026-11-18. In this repo:

- `scripts/rotate-and-seal.sh` wraps the portable `rotate-and-seal.sh` with this project's
  settings: `ENV_FILE=/home/dblagbro/docker/.env`, a single-container recreate of `llm-proxy2`
  as `RECREATE_CMD`, and a fixed list of rotatable variable names. It does nothing unless
  invoked with a variable name, and needs `GPG_RECIPIENT`. Read its header before rotating:
  the gateway has no versioned-key support, and the `.env` file is per node.
- The ignore patterns (`secrets-sealed-*`, `*credentials-backup*`, `*.key`, alongside the
  existing `.env*`, key-material and database patterns) are kept in `.gitignore`,
  `.dockerignore`, `frontend/.gitignore`, `frontend/eslint.config.js` and
  `BLOCKED_PATH_RE` in `.githooks/_lib.sh` (used by the pre-commit and pre-push hooks).
  The project has no prettier ignore file.
