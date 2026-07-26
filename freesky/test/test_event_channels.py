"""Event/PPV feeds live only on the homepage schedule, not on 24-7-channels.php.

Checks the merge that pulls them into the channel list: ids already on the 24/7
page are skipped, an id reused under two titles keeps the first, and duplicate
names get their id appended so they're distinguishable in the UI/playlist.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from freesky.free_sky_hybrid import StepDaddyHybrid

HOME_PAGE = """
<a href="/watch.php?id=69" title="DAZN PPV" data-ch="dazn ppv">DAZN PPV</a>
<a href="/watch.php?id=59" title="PPV Feed">PPV Feed</a>
<a href="/watch.php?id=59" title="DAZN PPV">DAZN PPV</a>
<a href="/watch.php?id=230" title="DAZN 1 UK">DAZN 1 UK</a>
<a href="/watch.php?id=901" title="Backup Stream">Backup Stream</a>
<a href="/watch.php?id=902" title="Backup Stream">Backup Stream</a>
"""


class _FakeResponse:
    status_code = 200
    text = HOME_PAGE


class _FakeSession:
    async def get(self, *args, **kwargs):
        return _FakeResponse()


def test_event_channels():
    sd = StepDaddyHybrid()
    sd._session = _FakeSession()
    # 230 already came from 24-7-channels.php
    channels = asyncio.run(sd._load_event_channels({"230"}))
    by_id = {c.id: c.name for c in channels}

    assert "69" in by_id, "DAZN PPV (homepage-only) must be merged in"
    assert "230" not in by_id, "channels already in the 24/7 list must not duplicate"
    assert by_id["59"] == "PPV Feed", "first title wins for a reused id"
    assert sorted([by_id["901"], by_id["902"]]) == ["Backup Stream", "Backup Stream (902)"], \
        f"duplicate names must be disambiguated, got {by_id}"
    print(f"OK: {len(channels)} event channels merged -> {by_id}")


if __name__ == "__main__":
    test_event_channels()
