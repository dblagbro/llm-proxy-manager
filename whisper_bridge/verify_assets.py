"""Verify the complete pinned speech-asset tree without network access."""

import hashlib
import json
from pathlib import Path

expected = json.loads(Path(__file__).with_name("assets-manifest.json").read_text())
actual = {}
for root in ["/models", "/voices", "/opt/piper", "/root/.cache/huggingface"]:
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            actual[str(path)] = {"symlink": str(path.readlink())}
        elif path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            actual[str(path)] = {"bytes": path.stat().st_size, "sha256": digest}
if actual != expected:
    raise SystemExit("Speech asset manifest mismatch")
print(f"Verified {len(actual)} pinned speech asset entries")
