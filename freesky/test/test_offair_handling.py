"""Off-air channels must fail fast, be remembered, and be visible to the monitor.

Regression cover for the 2026-09 incident: 18 of 33 channels were off air
upstream, and every request for one cost 22s and returned 504 while
/health reported 18/18 channels healthy.
"""
import time

from freesky.stream_monitor import StreamMonitor


def test_failure_is_recorded():
    """A failed attempt must move the metrics — it previously moved nothing."""
    m = StreamMonitor()
    m.record_stream_attempt("588", False, 22.0)

    assert m.metrics["588"].success_rate == 0.0
    assert m.metrics["588"].consecutive_failures == 1
    assert m.is_stream_healthy("588") is False


def test_consecutive_failures_count_up_and_reset_on_success():
    m = StreamMonitor()
    for _ in range(3):
        m.record_stream_attempt("588", False, 0.0)
    assert m.metrics["588"].consecutive_failures == 3

    m.record_stream_attempt("588", True, 0.4)
    assert m.metrics["588"].consecutive_failures == 0


def test_breaker_fires_for_a_channel_that_never_succeeded():
    """The core bug: last_success defaults to 0, so the backoff compared against
    the epoch and should_skip_channel never returned True for exactly the
    channels it exists to protect."""
    m = StreamMonitor()
    for _ in range(m.max_consecutive_failures):
        m.record_stream_attempt("588", False, 0.0)

    assert m.should_skip_channel("588") is True


def test_breaker_releases_once_the_backoff_window_passes():
    m = StreamMonitor()
    for _ in range(m.max_consecutive_failures):
        m.record_stream_attempt("588", False, 0.0)
    assert m.should_skip_channel("588") is True

    # First window is 60s; pretend the last attempt was longer ago than that.
    m.last_failures["588"] = time.time() - 61
    m._update_metrics("588")
    assert m.should_skip_channel("588") is False


def test_backoff_grows_with_repeated_failures():
    m = StreamMonitor()
    for _ in range(m.max_consecutive_failures + 1):
        m.record_stream_attempt("588", False, 0.0)

    # One failure past the threshold doubles the window to 120s, so a channel
    # last tried 61s ago is still inside it.
    m.last_failures["588"] = time.time() - 61
    m._update_metrics("588")
    assert m.should_skip_channel("588") is True


def test_healthy_channel_is_never_skipped():
    m = StreamMonitor()
    for _ in range(5):
        m.record_stream_attempt("44", True, 0.4)

    assert m.should_skip_channel("44") is False
    assert m.is_stream_healthy("44") is True


def test_unknown_channel_is_not_skipped():
    """No data is not evidence of failure — a new channel must get its chance."""
    assert StreamMonitor().should_skip_channel("999") is False


def test_summary_reports_the_outage():
    """/health claimed 18/18 healthy through an 18-channel outage."""
    m = StreamMonitor()
    for _ in range(3):
        m.record_stream_attempt("588", False, 0.0)
    m.record_stream_attempt("44", True, 0.4)

    summary = m.get_metrics_summary()
    assert summary["total_channels"] == 2
    assert summary["healthy_channels"] == 1
    assert summary["health_rate"] == 0.5


def test_offair_error_is_a_valueerror():
    """Existing `except ValueError` handlers around playlist fetching must keep
    catching it, so introducing the type cannot change old behaviour."""
    try:
        from freesky.free_sky_hybrid import ChannelOffAirError
    except ImportError:  # reflex not installed in this environment
        print("  (skipped: reflex unavailable)")
        return

    assert issubclass(ChannelOffAirError, ValueError)


def test_each_failure_kind_keeps_its_own_response():
    """A cached failure must replay what the live attempt returned. Caching only
    the reason string made a timeout report itself as "not currently
    broadcasting", which read as a total outage during the 2026-09 incident."""
    try:
        from freesky import backend
    except Exception:  # reflex not installed in this environment
        print("  (skipped: reflex unavailable)")
        return

    offair = backend._failure_response("588", backend._FAILURE_OFFAIR, "HTTP 404 from CDN")
    timeout = backend._failure_response("588", backend._FAILURE_TIMEOUT)
    missing = backend._failure_response("588", backend._FAILURE_NOT_FOUND)

    assert offair.status_code == 404
    assert timeout.status_code == 504   # honest: we timed out, we did not learn it is off air
    assert missing.status_code == 404
    # The three must not be mistakable for one another.
    bodies = {r.body for r in (offair, timeout, missing)}
    assert len(bodies) == 3
    assert b"not currently broadcasting" in offair.body
    assert b"not currently broadcasting" not in timeout.body


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("off-air handling ok")
