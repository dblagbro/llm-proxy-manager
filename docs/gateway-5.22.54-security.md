# Gateway 5.22.54 security preparation

Status: exact candidate tests, scan and all six CI checks pass at code head
f6a713612437b7fc60c9c6d10138c180d85acae2. The lint policy decision,
per-node runtime recovery rehearsal and deployment remain pending.
This is not a deployed fix. Production is still 5.22.53 on both hosts.

## Source reconciliation

GitHub main 5c560b3 contained gateway 5.22.42 plus Whisper 4.3.1. The maintained
gateway checkout at 881695d contained six unpublished commits through 5.22.53.
Preparation merge00bd593 reconciled both histories. The final PR snapshot
backfills that same deployed code without rewriting the canonical branches. Before the version bump, its app, frontend and Grok source trees
were byte-identical to 881695d. The canonical checkout's uncommitted operational
notes are excluded. These existing deployed fixes must not be lost by building
an older GitHub tree.

## Bounded dependency change

No application jose imports or installed-package dependents were found. Ecdsa's
only declared dependent was python-jose. Remove both from the overlay; remove
python-jose from both maintained dependency manifests. Pyproject's obsolete
passlib dependency is corrected to bcrypt, already used by production. Align
its LiteLLM bounds with requirements.txt; no LiteLLM version upgrade is claimed.

Supported distribution updates and pip26.3 are applied. The affected msgpack
and urllib3 copies are vendored inside pip, so installing separate top-level
packages does not fix them. Pip26.3 updates those bundled libraries. The
maintained build metadata also sets safe setuptools/wheel floors; those build
tools are not added to the runtime. Future clean builds upgrade pip explicitly.
The overlay otherwise retains the resolved application dependencies/frontend.

The first tested candidate is retained as an unsuccessful security iteration:
its tests passed and scan reached0 Critical/55 High, but adding setuptools78.1.1
introduced older wheel/jaraco.context bundles and did not repair pip's copies.
It was never deployed. The revised overlay fixes the supplying package instead.
Pip still labels its vendored pkg_resources subset as setuptools70.3.0; assess
that finding against the actual included code, without deleting scanner evidence.

Base image ID: sha256:21a85131e8b68dbdcd13c5bc0d7a5ec56fd971565ed3d481eadb1b75e33cd900.
Verify the local tag tmr-security/llm-proxy-parent-test-base:20261109 resolves
to this exact ID before building. It must already exist locally and remain retained for rollback. This local
content identity is not an assertion that a public registry can fetch it.

```sh
make build GATEWAY_IMAGE=tmr-security/llm-gateway:5.22.54-20261110-v2 \
  GATEWAY_DOCKERFILE=Dockerfile.security \
  GATEWAY_BASE_IMAGE=tmr-security/llm-proxy-parent-test-base:20261109
```

Run through the estate capacity guard with an 8 GiB peak estimate and at least
64 GiB reserved. Record the resulting image, resolved package inventory and
scan; apt repositories can change, so the Dockerfile alone does not establish
bit-for-bit reproducibility. Deploy only an exact tested image identity.

## Gates and rollout

The repository requires clean make lint, passing make test, isolated app import,
secret scanning, and final-head/main CI. Published main has 3,244 lint errors;
the existing Whisper exception explicitly excludes gateway releases. No waiver
is inferred from general deployment authorization. Do not expand known failures
or suppress annotations. Record candidate and baseline results separately.

Before deployment: consistent per-node database/config backups, isolated restore
and original-image rollback verification, candidate scan, unchanged app/frontend
comparison, and authenticated/anonymous route acceptance. No provider or live
account transactions belong in the test environment. Keep SSO disabled.

Use the canonical root stack, peer first, one llm-proxy2 service at a time:
docker compose up -d --force-recreate --no-deps llm-proxy2; validate and reload
Nginx, verify public subpath/private health/version/auth, then soak. Never bring
down the stack. Original per-node images, configuration, volumes and backups
remain retained. Restore the original service image/config if acceptance fails;
do not blindly restore data after new writes.

## Residual findings

Python-jose package removal addresses its presence, not a proven live exploit.
LiteLLM 1.86.7's Proxy credential-file advisory remains a retained finding until
deployed-handler reachability tests are complete; static library-only evidence
is insufficient for blanket exemption. SSO remains disabled; complete OIDC
validation deserves separate review before enablement. Unfixed Debian findings
remain visible. Fresh candidate results appear below; production is unchanged.

Sources: https://github.com/advisories/GHSA-3qf3-8w2g-rqmx ;
https://github.com/BerriAI/litellm/security/advisories/GHSA-g5ff-637f-6q2m ;
https://github.com/urllib3/urllib3/releases/tag/2.8.0

## Verified candidate checkpoint (2026-11-10)

Candidate13771511ed12 passes make test (4,101 passed,2 skipped), the required
hermetic repetition (same totals), dependency consistency, exact-image import,
password roundtrip and SSO-disabled/route checks. Its fresh scan is0 Critical /
49 High, versus retained production1 /58. The earlier candidate's0 /55 scan
is retained as superseded; it was not deployed. No scan is called vulnerability
free and no unchanged production scan was rerun.

All307 application/frontend files match both deployed nodes except the expected
version-file change. Image configuration is identical. The three stale SQLAlchemy
test doubles are updated to the already-deployed row/scalar interfaces; behavior
assertions remain, and those files pass scoped lint/format. The isolated runner
uses a temporary bytecode directory and installs the declared Playwright test
package. Initial failures remain recorded; known_failures is unchanged.

Global lint remains3,252 findings versus3,256 on the reconciled baseline, with
zero introduced findings. Published5.22.42's earlier baseline was3,244. These are
different source baselines, not contradictory counts. This is not a green global
gate, and the existing sidecar exception does not apply.

The49 raw High matches include Debian findings, retained LiteLLM applicability,
and pip's setuptools70.3 label. Pip's documented vendoring strips setuptools to
pkg_resources; the tested image has no setuptools directory or package_index.py.
The PackageIndex.download code named by CVE-2025-47273 is absent from this subset.
Keep the raw finding visible with this applicability evidence. Do not delete
vendor metadata or the standard-library bootstrap wheel to conceal findings.

The exact inventory, recipe hash, scan records and sanitized log hashes are in
[gateway-5.22.54-verification.json](gateway-5.22.54-verification.json).
Source: https://raw.githubusercontent.com/pypa/pip/26.3/src/pip/_vendor/README.rst ;
https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf

GitGuardian flagged the inherited redaction tests' explicit synthetic bearer
strings. They now generate nonfunctional samples at test time with the same
redaction assertions. The security PR branch is rebuilt as a clean snapshot
from main so its old fixture-only findings do not remain in the PR history.
Canonical history and working edits are retained. No real token was identified.

## Recovery and release decision checkpoint

On November10, both nodes passed online SQLite backup integrity checks and
independent file restoration. Private NAS archives were independently read back
and hash-verified. These are per-node point-in-time backups, not a quiesced
cluster snapshot. Refresh them at cutover. Candidate runtime restore and
original-image rollback boot have not yet been exercised. Never restore these
preparation copies over newer production writes.

All six GitHub checks at f6a7136 pass: GitGuardian, secrets, guard, ephemeral
integration, Playwright UI and speech runtime. Global lint remains failing with
3,252 inherited findings versus 3,256 on the reconciled deployed baseline, and
zero introduced findings. The owner has been asked to approve a one-release
5.22.54 baseline-no-new lint contract; no answer or waiver is assumed. PR12
remains draft. All other recovery, rollback and sequential live acceptance
gates remain required even if that narrow lint exception is approved.
