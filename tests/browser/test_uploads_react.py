"""React-controlled inputs on the localhost mock ATS (fictional employer, fictional data),
on the ``react-controlled`` form: the page keeps every value in its own state, updated
only by trusted input events, resets any control that differs from that state, and
re-renders the whole field block once after the first change (the controls are removed
behind a busy "Saving draft…" marker and mounted again from state).

The runtime types with real input events, waits the transient re-render out, finds the
new controls again by field id and types a value the re-render dropped once more, so the
candidate's values end up in the DOM and in the page's state alike. Real headless
Chromium; nothing is ever submitted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_core import BrowserOptions, FieldFillStatus

REACT = "/jobs/react-controlled/apply"
IDS = ["first_name", "last_name", "email", "phone", "resume"]
OURS = {
    "first_name": "Avery", "last_name": "Quill", "email": "avery.quill@example.test",
    "phone": "+1 (303) 555-0142",
}
RESUME_FILE = {"name": "resume_avery_quill.pdf", "size": 802}
MOCK = "() => JSON.parse(JSON.stringify({...window.__mock, files: undefined}))"
DOM = """(names) => ({
  values: Object.fromEntries(names.map((name) => {
    const control = document.getElementById('f-' + name);
    return [name, control ? control.value : null];
  })),
  saving: document.getElementById('react-saving') !== null,
  files: Array.from(document.getElementById('f-resume').files)
    .map((f) => ({name: f.name, size: f.size})),
})"""


async def _fill(kit: SimpleNamespace, options: BrowserOptions,
                url: str) -> tuple[Any, Any, dict[str, Any], dict[str, Any]]:
    browser = await PlaywrightSessionFactory().start(options)
    try:
        page = await browser.open(url)
        assert page.form is not None, page.message
        fill = await browser.fill(page.form, kit.build(page.form, kit.pick(page.form, kit.CORE)).packet)
        # Several reconcile passes (every 150 ms) later, nothing has been reset.
        await asyncio.sleep(0.6)
        return page, fill, await browser.page.evaluate(MOCK), await browser.page.evaluate(DOM, list(OURS))
    finally:
        await browser.close()


def _assert_values_stick(page: Any, fill: Any, mock: dict[str, Any], dom: dict[str, Any]) -> list[str]:
    assert [f.id for f in page.form.fields] == IDS
    assert fill.ok, [f for f in fill.fields if f.status is not FieldFillStatus.FILLED]
    assert {f.field_id: f.status for f in fill.fields} == dict.fromkeys(IDS, FieldFillStatus.FILLED)
    events = [entry["event"] for entry in mock["log"]]
    assert mock["renders"] == 1 and events.count("rerender") == 1, events
    # The page's own state (fed only by trusted input events) holds what the DOM shows.
    assert mock["state"] == OURS
    assert dom["values"] == OURS and not dom["saving"]
    assert dom["files"] == [RESUME_FILE]
    return events


def test_typed_values_survive_the_rerender_in_the_dom_and_in_page_state(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    page, fill, mock, dom = kit.run(_fill(kit, options, server.url(REACT)))
    _assert_values_stick(page, fill, mock, dom)
    assert server.submissions("react-controlled")["accepted_count"] == 0


def test_a_value_the_rerender_dropped_is_typed_again(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    # The first trusted input never reaches the page's state (typed "before hydration"),
    # so the re-render mounts the first name empty again.
    page, fill, mock, dom = kit.run(_fill(kit, options, server.url(REACT + "?lose_first=1")))
    events = _assert_values_stick(page, fill, mock, dom)
    rerender = events.index("rerender")
    assert [i for i, event in enumerate(events) if event == "input:first_name" and i > rerender], events
    assert server.submissions("react-controlled")["accepted_count"] == 0


def test_an_unmount_window_overlapping_later_writes_is_waited_out(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    # After the first change the controls stay unmounted for 600 ms, long enough to cover
    # the writes of the fields after it.
    page, fill, mock, dom = kit.run(_fill(kit, options, server.url(REACT + "?unmount_ms=600")))
    _assert_values_stick(page, fill, mock, dom)
    assert server.submissions("react-controlled")["accepted_count"] == 0
