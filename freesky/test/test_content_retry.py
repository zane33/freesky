"""Transient upstream failures must be retried, not turned into a 500.

A connect timeout on a playlist fetch used to kill the whole stream because
players read a 500 as "channel is dead".
"""
import asyncio
import types

import httpx

from freesky import backend


def _fake_client(outcomes):
    """Client whose get() replays `outcomes` (exceptions raised, responses returned)."""
    calls = []

    async def get(url, headers=None, timeout=None):
        calls.append(url)
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    return types.SimpleNamespace(get=get), calls


def _ok(body=b"#EXTM3U"):
    return httpx.Response(200, content=body)


def test_retries_transient_then_succeeds():
    client, calls = _fake_client([httpx.ConnectTimeout(""), httpx.Response(502), _ok()])
    backend.streaming_client = client
    resp = asyncio.run(backend._get_upstream_with_retry("http://x/a.m3u8", {}))
    assert resp.status_code == 200
    assert len(calls) == 3


def test_gives_up_after_max_attempts():
    client, calls = _fake_client([httpx.ConnectTimeout("")] * backend._UPSTREAM_ATTEMPTS)
    backend.streaming_client = client
    try:
        asyncio.run(backend._get_upstream_with_retry("http://x/a.m3u8", {}))
        assert False, "should have raised"
    except httpx.ConnectTimeout:
        pass
    assert len(calls) == backend._UPSTREAM_ATTEMPTS


def test_hard_failure_is_not_retried():
    client, calls = _fake_client([httpx.Response(403)])
    backend.streaming_client = client
    try:
        asyncio.run(backend._get_upstream_with_retry("http://x/a.m3u8", {}))
        assert False, "should have raised"
    except ValueError as e:
        assert "403" in str(e)
    assert len(calls) == 1


def test_describe_names_empty_message_errors():
    assert backend._describe(httpx.ConnectTimeout("")) == "ConnectTimeout"


if __name__ == "__main__":
    test_retries_transient_then_succeeds()
    test_gives_up_after_max_attempts()
    test_hard_failure_is_not_retried()
    test_describe_names_empty_message_errors()
    print("ok")


class _FakeStream:
    """Stand-in for httpx.AsyncClient.stream(): an async context manager whose
    __aenter__ replays `outcomes` (an exception or an httpx.Response)."""

    def __init__(self, outcomes, closed):
        self.outcomes, self.closed = outcomes, closed

    async def __aenter__(self):
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def __aexit__(self, *a):
        self.closed.append(True)


def _segment_request(outcomes):
    closed = []
    client = types.SimpleNamespace(
        stream=lambda *a, **k: _FakeStream(outcomes, closed),
        get=None,
    )
    backend.streaming_client = client
    path = backend.encrypt("http://cdn/x/1.ts")
    req = types.SimpleNamespace(query_params={})
    return asyncio.run(backend.content(path, req)), closed


def test_segment_403_is_relayed_not_crashed():
    """A non-200 upstream must become a real status before headers go out —
    raising inside the body generator crashed the connection mid-stream."""
    backend.stream_cache["stream_1"] = ("#EXTM3U", 0)
    resp, closed = _segment_request([httpx.Response(403)])
    assert resp.status_code == 403
    assert closed, "upstream stream must be closed"
    assert "stream_1" not in backend.stream_cache, "stale playlists dropped"
    assert not backend.active_content_sessions, "session must be released"


def test_segment_transient_then_ok_streams_body():
    ok = httpx.Response(200, content=b"seg")
    resp, closed = _segment_request([httpx.Response(503), ok])
    assert resp.status_code == 200
    body = b""

    async def drain():
        nonlocal body
        async for chunk in resp.body_iterator:
            body += chunk
    asyncio.run(drain())
    assert body == b"seg" and len(closed) == 2
    assert not backend.active_content_sessions
