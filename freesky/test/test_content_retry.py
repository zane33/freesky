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


def _nested_request(path, ref=None):
    req = types.SimpleNamespace(query_params={"token": "t"})
    return asyncio.run(backend.content(path, req, ref))


def _fail_client(outcomes):
    client, calls = _fake_client(outcomes)
    backend.streaming_client = client
    return calls


def test_nested_playlist_serves_recent_stale_copy_on_cdn_failure():
    """ffmpeg (no -reconnect) dies on the first non-200 playlist reload, so a
    CDN hiccup must be papered over with the last good copy."""
    path = backend.encrypt("http://cdn/live/mono.m3u8")
    backend._nested_cache.clear(); backend._nested_alias.clear()
    _fail_client([httpx.Response(200, content=b"#EXTM3U\nhttp://cdn/live/1.ts\n")])
    assert _nested_request(path).status_code == 200 and path in backend._nested_cache
    _fail_client([httpx.ConnectTimeout("")] * backend._NESTED_ATTEMPTS)
    resp = _nested_request(path)
    assert resp.status_code == 200
    assert b"/api/content/" in resp.body and b"token=t" in resp.body


def test_nested_playlist_fails_over_to_new_feed():
    """No usable stale copy -> re-resolve the channel and serve the NEW feed's
    media playlist on the OLD url; later reloads are aliased to the new feed."""
    old = backend.encrypt("http://cdn-a/old.m3u8")
    backend._nested_cache.clear(); backend._nested_alias.clear()
    backend._content_channel[old] = "42"
    new_master = "#EXTM3U\nhttp://cdn-b/new.m3u8\n"

    async def fake_resolve(channel_id, prefer=None):
        assert channel_id == "42"
        return new_master
    orig = backend._get_stream_parallel
    backend._get_stream_parallel = fake_resolve
    try:
        # old feed dead, new feed answers
        _fail_client([httpx.ConnectTimeout("")] * backend._NESTED_ATTEMPTS
                     + [httpx.Response(200, content=b"#EXTM3U\nhttp://cdn-b/9.ts\n")])
        resp = _nested_request(old)
        assert resp.status_code == 200 and b"9.ts" not in resp.body and b"/api/content/" in resp.body
        assert old in backend._nested_alias, "old url must now alias the new feed"
        # a reload of the OLD url goes straight to the new feed (one upstream call)
        calls = _fail_client([httpx.Response(200, content=b"#EXTM3U\nhttp://cdn-b/10.ts\n")])
        assert _nested_request(old).status_code == 200 and len(calls) == 1
        assert "cdn-b" in calls[0]
    finally:
        backend._get_stream_parallel = orig
