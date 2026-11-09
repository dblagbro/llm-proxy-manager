# Whisper bridge 4.3.1 security candidate

This is an independent voice-sidecar release. It does not change the gateway version, application database, gateway image, or frontend. The sidecar is stateless and has no published host port; the gateway calls it over the Docker network with a bridge token.

## Reproducible source and build

`whisper_bridge/Dockerfile` pins the Python 3.13 base and the existing speech-asset image by registry digest. It copies the existing Whisper model, Vosk archive, Piper executable/libraries and Amy voice instead of fetching mutable model URLs. `assets-manifest.json` records all 381 files/symlinks. All 43 Python packages are versioned and SHA256-locked for Linux x86_64 / CPython3.13; installation requires wheels and hashes. Debian updates are applied at build time, so a future rebuild can contain newer OS fixes and must be scanned and tested again.

```sh
make build-whisper
make test-whisper
make lint-whisper
make secret-scan
```

The image name can be supplied as `WHISPER_IMAGE=...`. The test target uses no network, production credentials, application data or paid provider calls. Tests exercise authentication, size limits, invalid audio, real speech synthesis, real transcription, a speech round trip, and the Vosk archive. The second test command verifies the complete speech-asset manifest.

The dependency resolver initially selected PyAV19, which removed an argument used by faster-whisper1.2.1. Two real decode tests failed before deployment. `av>=17.0.1,<18` resolves to17.1.0 and restores compatibility. Keep the real audio checks when relaxing this constraint.

## Evidence and limits

Candidate image: `sha256:e39e2fafbe718c31cec29450db7858ca49b2b82601085b363dbc16ed6bf83cda`.
Runtime recipe commit: `9c17771560dfb1747040a0a322f1b3d06d2a7483`.

The original and candidate each passed11 offline runtime tests, with zero failures/skips and a clean `pip check`. All381 speech assets match. The fresh candidate High/Critical scan reports1Critical/226High, versus the earlier original-image baseline16Critical/387High per host. The remaining Critical is Debian libxml2 CVE-2026-6653 without a scanner-listed fixed package. The other OS matches also lack listed fixes; the setuptools70.3 match is embedded SBOM metadata while the installed Python distribution is84.2.1. No finding is dismissed solely because the service is private. The retained native Piper bundle is unchanged and is not claimed comprehensively audited. Lower severities were outside this scan.

The isolated gateway import passed, and published-main CI was green before this change. Global `make lint` reports3244 pre-existing findings in gateway `app/` and `tests/`, which were byte-identical to published main at the baseline lint check. Sidecar test lint passes. Three existing Dockerfile guard tests are now adapted to check pinned asset copying and hashes instead of the previous download commands; the first CI run exposed those stale assertions (4042 passed,3 failed,4 skipped), and the corrected touched-area make test passed3/3 offline. Gateway application code remains unchanged. Global lint is not reported as green, and the project's release-gate decision must be resolved before deployment/merge. This document describes a tested candidate, not a completed rollout.

## Sequential deployment and recovery

Use the canonical Compose stack on each node. Preserve the existing environment, token, network, restart policy, mounts and service configuration. Save a root-only copy of the Compose configuration and original container inspection; do not place those secret-bearing files in Git. Retain the original image and confirm its isolated offline tests before changing production.

Load the exact tested image on the peer first and verify its image ID. Update only the `whisper-bridge` image reference in canonical Compose, validate the resolved service, and recreate only that service with `docker compose up -d --force-recreate --no-deps whisper-bridge`. Verify local health, anonymous rejection on all protected routes, authenticated synthetic speech/transcription, unchanged gateway health/version and unchanged service configuration. Check gateway reachability to the recreated sidecar. Then repeat on the primary and observe health before completion. Brief speech requests to the node being changed may need retry; the other gateway and unrelated services remain running.

If a node fails acceptance, restore only that service's original image reference and recreate only `whisper-bridge`; verify health and synthetic speech again. It has no persistent application data and needs no database restoration. Never stop the full stack or remove volumes. Record per-node deployed image IDs, actual checks and any rollback separately from source/PR/check/merge states.
