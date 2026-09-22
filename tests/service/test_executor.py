"""Executor shutdown lets a cancelled run finish its awaited cleanup."""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from typing import Any

from interviewmaxxing_core import ApplicationStore, ApplyOutcome, LocalPaths
from interviewmaxxing_service import Dispatcher, ServiceInteraction


class SlowRunner:
    """Holds a real store claim and a fake browser; cleanup is awaited in ``finally``."""

    def __init__(self, paths: LocalPaths, events: dict[str, threading.Event]) -> None:
        self.paths = paths
        self.events = events

    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome:
        with ApplicationStore.open(self.paths.state_db) as store:
            app = store.record_request(candidate_id, application_url).application
            claim = store.claim(app.id, "slow-runner", ttl=timedelta(minutes=5))
            try:
                self.events["started"].set()
                await asyncio.sleep(3600)
                raise AssertionError("not reached")
            finally:
                await asyncio.sleep(0.05)  # e.g. ``await browser.close()``
                self.events["browser_closed"].set()
                store.release(claim)
                self.events["claim_released"].set()

    async def resume(self, application_id: str) -> ApplyOutcome:
        raise NotImplementedError

    async def reconcile(self, application_id: str) -> ApplyOutcome:
        raise NotImplementedError


def test_shutdown_awaits_cancelled_cleanup(isolated_imx_home: LocalPaths) -> None:
    paths = isolated_imx_home
    paths.ensure()
    events = {k: threading.Event() for k in ("started", "browser_closed", "claim_released")}

    def factory(interaction: ServiceInteraction) -> Any:
        return SlowRunner(paths, events)

    dispatcher = Dispatcher(factory)
    run = dispatcher.submit(
        "app_x", "apply",
        lambda ex: ex.apply("https://jobs.example.test/slow/1", candidate_id="default"),
    )
    assert events["started"].wait(5)
    dispatcher.shutdown(timeout=0.1)
    assert events["browser_closed"].is_set()
    assert events["claim_released"].is_set()
    assert run.done.is_set()
    with ApplicationStore.open(paths.state_db) as store:
        (app,) = store.list_applications()
        assert app.claim_owner is None
