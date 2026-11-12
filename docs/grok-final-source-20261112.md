# Final Grok authorization and release checker reconciliation

The final merged PR13 source restricts the machine token to inference APIs.
The previously prepared image and deployment checker predate that correction.
The old checker expected token-only access to `/api/status`; correct final
authorization returns401 and would make that checker reject the deployment.

The corrected checker requires401 on status with and without a machine token.
An empty `/api/chat` request must return401 with no or wrong token and the exact
early validation400 with a valid token. That validation precedes browser and
provider actions. No account, conversation or credential value is exported.
Thirteen regression cases execute the actual generated probe with synthetic
HTTP responses and reject the old browser-token bypass and false-positive400s.

New release generations now require the separately mounted SAN. Existing
`/var/backups/codex-grok-releases` generations remain valid for rollback, without
relocation or permission changes. Lifecycle tests allow only new, narrowly named
SAN attempt directories. Sixteen tests exercise storage/path refusal and legacy
rollback behavior. This does not approve further cleanup.

The new candidate is
`sha256:587311da177cc015292981a01649226c8b909e8310d74fdbab481175cb2b16e9`.
All six runtime files match final PR13 head8de8cd02be65f8242d3070fbb6ee6209d3bd421c.
Only `bridge_security.py` differs from retained parent2d6e568; image Config is
identical and one COPY layer was added. The old image and evidence remain retained.

Validation on November12: 67 offline final-source authorization tests pass;
64 operator tests pass on Windows/Python3.12 and Linux/Python3.10. The Linux
operator environment reports the retained unused asyncio_mode configuration
warning. All11 isolated browser checks passed against gateway5.22.57 image233b7c
and Nginx67a105, including real gateway cookies, nonadmin denial, noVNC rendering,
keyboard input and logout revocation in27.7seconds. The gateway fixture uses
the real authentication router in a minimal application; full production
middleware, background workers and provider/account acceptance are not covered.

The scan and corrected operator's Docker lifecycle evidence are recorded
separately when complete. Earlier candidate recovery/scans are historical and
are not relabeled as tests of this new image. All example approval gates remain
false. Production still uses original Grok imagebe048c4.

The Grok-specific global lint/release-policy decision remains unresolved.
Neither PR13's merge nor the earlier Whisper/gateway exception clears it.
Current full configuration validation, fresh cutover snapshot/private recovery
and production acceptance also remain prerequisites. Chromium root/no-sandbox
and login-time role retention remain limitations.
