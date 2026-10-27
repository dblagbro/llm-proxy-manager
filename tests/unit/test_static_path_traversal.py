"""Regression: the SPA asset-rescue branch must not serve files outside
frontend/dist.

Before the fix, ``@app.get("/{full_path:path}")`` matched ``assets/...`` with a
regex whose group could contain ``../`` and joined it straight onto _static_dir,
giving an unauthenticated arbitrary file read that reached the on-disk SQLite
credential store (``/x/assets/../../../data/llmproxy.db``). The confinement
helper must reject any path that escapes the static root.
"""
from __future__ import annotations

import os

import app.main as main


def test_confined_static_file_blocks_traversal():
    root = main._static_root
    escapes = [
        "assets/../../../../etc/passwd",
        "assets/../../app/config.py",
        "assets/../../data/llmproxy.db",
        "../etc/passwd",
        "assets/\x00/etc/passwd",
    ]
    for rel in escapes:
        out = main._confined_static_file(rel)
        assert out is None or out == root or out.startswith(root + os.sep), (
            f"path escaped static root: {rel!r} -> {out!r}"
        )
        # A traversal target must not resolve to a real file outside the tree.
        if out is not None:
            assert out.startswith(root + os.sep) or out == root


def test_confined_static_file_allows_legit_root_file():
    # favicon.svg ships in frontend/dist; if the build is present it resolves,
    # and in all cases the returned path (when not None) stays inside the root.
    out = main._confined_static_file("favicon.svg")
    if out is not None:
        root = main._static_root
        assert out == os.path.join(root, "favicon.svg")
