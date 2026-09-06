"""v5.22.15 — the disconnect watchdog must not eat request-body chunks.

Measured, not theorised. Posting the **identical** 3 MB body with the
**identical** signature to a peer's ``/cluster/sync`` six times returned
200,200,200,403,200,200. Same bytes, same secret, same endpoint — so the
403 "Invalid cluster signature" could not be a key mismatch or a
serialisation bug, and a size limit would have been deterministic. It is a
race, and bigger bodies simply lose more often because they span more polls.

The mechanism is Starlette's ``Request.is_disconnected()``::

    with anyio.CancelScope() as cs:
        cs.cancel()
        message = await self._receive()
    if message.get("type") == "http.disconnect":
        ...

The cancel scope only bites at a checkpoint. A chunk that is already buffered
comes back without one, so it is consumed — and since only ``http.disconnect``
is inspected, an ``http.request`` chunk is silently dropped. The handler's
``await request.body()`` then returns short.

This was not a cluster-only problem. The watchdog is a dependency on
``/v1/messages``, ``/v1/chat/completions``, ``/v1/responses``, audio, images
and integration chat — every body-bearing endpoint — so the same race could
truncate a caller's request.
"""

import asyncio

import pytest

from app.utils.disconnect_watchdog import _request_body_pending


class _FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


class _FakeRequest:
    def __init__(self, headers=None, body=None):
        self.headers = _FakeHeaders({k.lower(): v for k, v in (headers or {}).items()})
        if body is not None:
            self._body = body
        self.is_disconnected_calls = 0

    async def is_disconnected(self):
        self.is_disconnected_calls += 1
        return False


class TestBodyPendingPredicate:
    def test_pending_while_declared_body_is_unread(self):
        assert _request_body_pending(_FakeRequest({"content-length": "3145784"})) is True

    def test_not_pending_once_body_is_buffered(self):
        req = _FakeRequest({"content-length": "3145784"}, body=b"x" * 10)
        assert _request_body_pending(req) is False

    def test_not_pending_for_a_bodyless_request(self):
        assert _request_body_pending(_FakeRequest({})) is False
        assert _request_body_pending(_FakeRequest({"content-length": "0"})) is False

    def test_pending_for_chunked_transfer(self):
        """No content-length, so length alone cannot decide it."""
        req = _FakeRequest({"transfer-encoding": "chunked"})
        assert _request_body_pending(req) is True

    def test_chunked_but_already_buffered_is_not_pending(self):
        req = _FakeRequest({"transfer-encoding": "chunked"}, body=b"done")
        assert _request_body_pending(req) is False

    def test_malformed_content_length_does_not_crash(self):
        assert _request_body_pending(_FakeRequest({"content-length": "abc"})) is False

    def test_empty_buffered_body_still_counts_as_read(self):
        """b'' is a real, complete body — not 'unread'."""
        assert _request_body_pending(_FakeRequest({"content-length": "5"}, body=b"")) is False


class TestWatcherDefersWhileBodyStreams:
    """The predicate is only useful if the polling loop actually honours it."""

    @pytest.mark.asyncio
    async def test_is_disconnected_not_called_while_body_pending(self, monkeypatch):
        from app.utils import disconnect_watchdog as wd

        monkeypatch.setattr(wd, "_enabled", lambda: True)
        monkeypatch.setattr(wd, "_poll_interval_sec", lambda: 0.01)

        req = _FakeRequest({"content-length": "3145784"})  # never buffered
        gen = wd.watch_for_disconnect(req)
        await gen.__anext__()
        await asyncio.sleep(0.25)  # many poll intervals
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

        assert req.is_disconnected_calls == 0, (
            "watchdog polled the receive channel while the body was still "
            "streaming — this is the chunk-eating race"
        )

    @pytest.mark.asyncio
    async def test_is_disconnected_resumes_once_body_is_read(self, monkeypatch):
        from app.utils import disconnect_watchdog as wd

        monkeypatch.setattr(wd, "_enabled", lambda: True)
        monkeypatch.setattr(wd, "_poll_interval_sec", lambda: 0.01)

        req = _FakeRequest({"content-length": "10"})
        gen = wd.watch_for_disconnect(req)
        await gen.__anext__()
        await asyncio.sleep(0.1)
        assert req.is_disconnected_calls == 0
        req._body = b"x" * 10  # handler finished reading
        await asyncio.sleep(0.25)
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

        assert req.is_disconnected_calls > 0, (
            "watchdog never resumed — disconnect detection would be lost"
        )
