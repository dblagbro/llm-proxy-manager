"""v5.0.22 / remediation Batch 2 — Grok bridge concurrency hardening.

Source-pin tests for the lock-acquisition contract. These do not
exercise live Playwright (no browser in the unit-test env); instead
they verify the lock pattern lives in the right call sites so future
edits don't silently drop it.

Pinned bugs:
  - BUG-052 chat() acquires _lock around the SPA-driven flow
  - BUG-055 _cookie_refresh_loop holds _lock around its _page.goto
  - BUG-065 lifespan teardown removes the context-level listener
            before closing the BrowserContext
"""
from __future__ import annotations

from pathlib import Path


_SRC = Path("grok_bridge/app.py")


def test_chat_endpoint_acquires_lock():
    """chat() must wrap its SPA-driven flow in `async with _lock:`
    (BUG-052). Without this, two concurrent /api/chat calls race on
    the textarea + button click + response listener and return
    swapped / corrupted content. Confirmed reproducer at sweep time.
    """
    src = _SRC.read_text()
    # Find the chat endpoint
    chat_start = src.find('@app.post("/api/chat")')
    assert chat_start != -1, "chat endpoint moved or removed"
    # Look for the next async with _lock: within ~5000 chars of the
    # endpoint definition (chat body is large, but the lock should be
    # acquired BEFORE _post_to_grok / _capture_statsig_id calls).
    chat_body = src[chat_start:chat_start + 6000]
    assert "async with _lock:" in chat_body, (
        "BUG-052 regression: chat() no longer wraps its SPA-driven "
        "flow in `async with _lock:`. Concurrent chats will race on "
        "the single Chromium tab."
    )


def test_cookie_refresh_loop_holds_lock_on_goto():
    """_cookie_refresh_loop's _page.goto must run under _lock
    (BUG-055). Without this, a refresh tick during a chat() navigates
    AWAY from the conversation page, producing the 'no usable
    textarea found' failure mode.
    """
    src = _SRC.read_text()
    fn_start = src.find("async def _cookie_refresh_loop")
    assert fn_start != -1
    # Find the next function header so we don't run into the next
    # function's body.
    next_def = src.find("async def ", fn_start + 1)
    fn_body = src[fn_start:next_def if next_def != -1 else fn_start + 4000]
    assert "async with _lock:" in fn_body, (
        "BUG-055 regression: _cookie_refresh_loop no longer acquires "
        "_lock around its _page.goto. A refresh tick during a chat() "
        "will reproduce the chat-failure-on-refresh-race symptom."
    )


def test_lifespan_removes_context_listener_before_close():
    """Lifespan teardown must call _context.remove_listener('request',
    _on_context_request) before closing the BrowserContext (BUG-065).
    Eliminates latent leak risk if the bridge ever runs multiple
    contexts or hot-reloads.
    """
    src = _SRC.read_text()
    teardown_idx = src.find("await _context.close()")
    assert teardown_idx != -1
    # Look in the ~600 chars immediately preceding the close call
    teardown_window = src[max(0, teardown_idx - 800):teardown_idx]
    assert "_context.remove_listener(" in teardown_window, (
        "BUG-065 regression: lifespan teardown no longer removes the "
        "context-level listener before closing the BrowserContext."
    )


def test_create_new_conversation_still_holds_lock():
    """create_new_conversation already held _lock pre-Batch 2; the
    refactor must not regress it. Pin as a contract test so a future
    edit can't silently drop it."""
    src = _SRC.read_text()
    fn_start = src.find("async def create_new_conversation")
    assert fn_start != -1
    next_def = src.find("@app.", fn_start + 1)
    body = src[fn_start:next_def if next_def != -1 else fn_start + 5000]
    assert "async with _lock:" in body, (
        "create_new_conversation no longer acquires _lock — Batch 2 "
        "regression on a pre-existing safety invariant."
    )
