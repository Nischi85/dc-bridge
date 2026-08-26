"""Regression test for the 2026-08-26 Interstellar false-negative: dc-bridge
only serialized hub_search calls *within* one item's own poll (title-variant/
episode pacing), not across different items polled concurrently. Two movies
searched moments apart landed close enough together that the hub's own flood
protection silently dropped one, producing a false "0 results" for a release
that was in fact plentifully available seconds later.

AirDCPP.hub_search now gates every dispatch (regardless of caller) behind a
shared minimum interval — this asserts that gate actually spaces concurrent
callers out instead of letting them fire together.
"""
import asyncio
import time

import httpx

from dcbridge.airdcpp import AirDCPP
from dcbridge.config import AirDCPPCfg


def _client(min_search_interval_seconds: float) -> AirDCPP:
    cfg = AirDCPPCfg(
        url="http://fake",
        username="u",
        password="p",
        min_search_interval_seconds=min_search_interval_seconds,
    )
    return AirDCPP(cfg)


def test_concurrent_hub_search_calls_are_spaced_apart(monkeypatch):
    ad = _client(min_search_interval_seconds=0.2)
    dispatch_times: list[float] = []

    async def fake_retry_on_401(method, path, **kw):
        dispatch_times.append(time.monotonic())
        return httpx.Response(200, request=httpx.Request(method, "http://fake" + path))

    monkeypatch.setattr(ad, "_retry_on_401", fake_retry_on_401)

    async def run():
        # Two different tracked items firing hub_search at (near) the same
        # instant — the exact shape of two concurrent poll_item runs.
        await asyncio.gather(
            ad.hub_search(1, "Interstellar"),
            ad.hub_search(2, "Coyote vs. Acme"),
        )

    asyncio.run(run())

    assert len(dispatch_times) == 2
    assert dispatch_times[1] - dispatch_times[0] >= 0.2


def test_first_hub_search_does_not_wait(monkeypatch):
    ad = _client(min_search_interval_seconds=5.0)

    async def fake_retry_on_401(method, path, **kw):
        return httpx.Response(200, request=httpx.Request(method, "http://fake" + path))

    monkeypatch.setattr(ad, "_retry_on_401", fake_retry_on_401)

    start = time.monotonic()
    asyncio.run(ad.hub_search(1, "Interstellar"))
    assert time.monotonic() - start < 1.0
