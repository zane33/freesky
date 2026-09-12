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
