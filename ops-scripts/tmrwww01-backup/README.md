# tmrwww01 nightly DB backup

`backup-safe-dumps.sh` is the nightly dump that has to cover **every** llm-proxy
database on the host, not just `/llm-proxy2/`'s. The live copy runs from
`/home/dblagbro/docker/scripts/backup-safe-dumps.sh`; this is the
version-controlled mirror.

## Why it is in the repo

`tests/unit/test_v510_batches_A_C1_B.py::test_backup_script_includes_clone_and_smoke`
asserts the script dumps the clone and smoke DBs, because an operator restore
that silently omits one loses that instance's distinct `api_keys` state.

Until v5.22.42 that test read the absolute host path
`/home/dblagbro/docker/scripts/backup-safe-dumps.sh`, so it could only pass on
one machine. It was the last non-hermetic thing in `tests/unit`, and it failed
the first time CI ran the full suite (v5.22.38) with a bare `FileNotFoundError`.
v5.22.38 made it skip; this makes it run everywhere.

Same pattern as `ops-scripts/tmrwww02-cert-hook/`: host-side scripts are
versioned here so they can be reviewed and tested without SSH.

## Keeping the two copies in step

The test now prefers this repo copy and falls back to the host path. If you edit
the live script, copy it back here — `test_backup_script_matches_host_copy`
reports drift when both are present, as a warning rather than a failure, since
the host is the one that actually runs.
