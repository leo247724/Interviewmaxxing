"""Offline tests for the OpenCLI page driver: argv, envelopes, errors, verification.

A scripted fake stands in for the ``opencli`` process (fictional data only), so these
tests never touch a real browser, profile or session. One test replays a real DOM
snapshot captured by headless Chromium from the localhost mock, to show the runtime
produces the same form (fingerprint) and the same refusals over the OpenCLI driver.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser import (
    CapabilityUnsupported,
    CommandResult,
    DriverError,
    NotActionable,
    OpenCliConfig,
    OpenCliDriver,
    OpenCliError,
    OpenCliSessionFactory,
    OpenCliTargetError,
    OpenCliTimeout,
    OpenCliUnavailable,
    PlaywrightSessionFactory,
    SubmissionRefused,
    UnverifiedAction,
    inspector_script,
)
from interviewmaxxing_browser import opencli as oc
from interviewmaxxing_browser import runtime as rt
from interviewmaxxing_core import (
    BrowserOptions,
    FieldFillStatus,
    NotSubmittedNext,
    SubmissionOutcome,
)

PAGE = "FAKE0000TAB1111"
URL = "http://127.0.0.1:9/jobs/demo/apply"
CONFIG = OpenCliConfig(profile="fixture-profile", navigation_grace_s=0.05, poll_interval_s=0.01)


class FakeOpenCli:
    """Answers ``opencli browser`` argv lists like the real CLI, from in-memory state."""

    def __init__(self) -> None:
        self.origin = "1000.5"
        self.url = URL
        self.status: int | None = 200
        self.controls: dict[str, dict[str, Any]] = {}
        self.snapshot: Any = None
        self.digests: dict[str, str | None] = {}
        self.tabs = [PAGE]
        self.created = PAGE
        self.native_invalid: list[str] = []
        self.upload_allowed = False
        self.navigate_on: set[str] = set()
        self.reidentify: set[str] = set()
        self.select_label_first: dict[str, str] = {}
        self.unavailable = False
        self.calls: list[list[str]] = []

    @staticmethod
    def ok(payload: Any) -> CommandResult:
        return CommandResult(0, json.dumps(payload) + "\n\n", "")

    @staticmethod
    def error(code: str, message: str) -> CommandResult:
        return CommandResult(1, json.dumps({"error": {"code": code, "message": message}}),
                             f"✖  [{code}] {message}\n")

    def control(self, sel: str) -> dict[str, Any]:
        return self.controls.setdefault(sel, {"value": "", "checked": False, "values": None, "files": []})

    def _eval(self, js: str) -> CommandResult:
        scripts = {
            "inspector": inspector_script(), "doc": oc._DOC_STATE, "control": oc._CONTROL_STATE,
            "actionable": oc._ACTIONABLE, "read": rt._READ_CONTROL, "checked": rt._READ_CHECKED,
            "validity": rt._NATIVE_VALIDITY, "identity": rt._DOCUMENT_IDENTITY,
            "effective": rt._EFFECTIVE_SUBMISSION, "digest": oc._FILE_DIGEST,
        }
        name = next((key for key, script in scripts.items() if "(" + script + ")(" in js), None)
        if name is None:
            return CommandResult(0, json.dumps({"ok": False, "error": "unknown script"}), "")
        marker = "(" + scripts[name] + ")("
        arg = json.loads(js[js.index(marker) + len(marker): js.rindex(")}); } catch")])
        value: Any
        if name == "inspector":
            value = self.snapshot
        elif name == "doc":
            value = {"origin": self.origin, "url": self.url, "ready": "complete", "status": self.status}
        elif name in ("control", "read"):
            c = self.controls.get(arg)
            value = None if c is None else {"origin": self.origin, "url": self.url, **c}
        elif name == "digest":
            value = {"origin": self.origin, "url": self.url, "sha256": self.digests.get(arg)}
        elif name == "actionable":
            value = {"n": 1, "disabled": False, "visible": True} if arg in self.controls else {"n": 0}
        elif name == "checked":
            value = [self.control(s)["checked"] for s in arg]
        elif name == "validity":
            value = self.native_invalid
        elif name == "identity":
            value = f"{self.origin} {self.url}"
        else:
            value = None
        return CommandResult(0, json.dumps({"ok": True, "value": value}) + "\n", "")

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if self.unavailable:
            return CommandResult(1, "", "✖  Daemon not running. Run `opencli doctor`.\n")
        i = argv.index("browser")
        command = argv[i + 2]
        positionals = argv[argv.index("--") + 1:] if "--" in argv else []
        if command == "tab":
            sub = argv[i + 3]
            if sub == "new":
                return self.ok({"page": self.created, "url": None})
            if sub == "list":
                return self.ok([{"page": p} for p in self.tabs])
            return self.ok({"closed": positionals[0]})
        if command == "open":
            self.url = positionals[0]
            return self.ok({"url": self.url, "page": PAGE})
        if command == "close":
            return self.ok({"closed": True})
        if command == "eval":
            return self._eval(positionals[0])
        sel = positionals[0] if positionals else ""
        if sel not in self.controls:
            return self.error("selector_not_found", f'CSS selector "{sel}" matched 0 elements')
        level = "reidentified" if sel in self.reidentify else "exact"
        if sel in self.navigate_on:
            self.origin = str(float(self.origin) + 1)
        c = self.control(sel)
        if command == "fill":
            c["value"] = positionals[1]
            return self.ok({"filled": True, "verified": True, "actual": positionals[1],
                            "matches_n": 1, "match_level": level})
        if command == "select":
            chosen = self.select_label_first.get(positionals[1], positionals[1])
            c["values"] = [chosen]
            c["value"] = chosen
            return self.ok({"selected": chosen, "matches_n": 1, "match_level": level})
        if command in ("check", "uncheck"):
            c["checked"] = command == "check"
            return self.ok({"checked": c["checked"], "changed": True, "matches_n": 1, "match_level": level})
        if command == "upload":
            if not self.upload_allowed:
                return CommandResult(1, "", '✖  {"code":-32000,"message":"Not allowed"}\n')
            path = Path(positionals[1])
            c["files"] = [{"name": path.name, "size": path.stat().st_size}]
            self.digests[sel] = hashlib.sha256(path.read_bytes()).hexdigest()
            return self.ok({"uploaded": True, "matches_n": 1, "match_level": level})
        if command == "click":
            return self.ok({"clicked": True, "matches_n": 1, "match_level": level})
        if command == "screenshot":
            Path(positionals[0]).write_bytes(b"\x89PNG fake")
            return CommandResult(0, f"Screenshot saved to: {positionals[0]}\n", "")
        return self.error("unknown", command)


def driver_with(fake: FakeOpenCli, config: OpenCliConfig = CONFIG) -> OpenCliDriver:
    return OpenCliDriver(config, runner=fake)


def commands(fake: FakeOpenCli) -> list[str]:
    return [c[c.index("browser") + 2] for c in fake.calls]


# --- argv and session safety ----------------------------------------------------------


def test_argv_uses_profile_session_pinned_tab_and_end_of_options(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    fake.control("#name")
    driver = driver_with(fake)

    async def scenario() -> None:
        await driver.goto(URL)
        await driver.fill("#name", "--tab OTHER; $(rm -rf ~)")

    kit.run(scenario())
    first = fake.calls[0]
    assert first == ["opencli", "--profile", "fixture-profile", "browser", driver.session,
                     "tab", "new", "--window", "background"]
    fill = next(c for c in fake.calls if "fill" in c)
    assert fill == ["opencli", "--profile", "fixture-profile", "browser", driver.session, "fill",
                    "--tab", PAGE, "--", "#name", "--tab OTHER; $(rm -rf ~)"]
    assert all("--tab" in c and c[c.index("--tab") + 1] == PAGE for c in fake.calls[2:])
    assert not {"bind", "unbind"} & set(commands(fake))


@pytest.mark.parametrize("session", ["imx-assessment-opencli", "imx-assessment", "imx-jobs-linkedin", "", "-x"])
def test_protected_or_invalid_sessions_are_refused(session: str) -> None:
    with pytest.raises(ValueError):
        OpenCliConfig(session=session)


def test_protected_tab_and_commands_before_open_are_refused(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    driver = driver_with(fake, OpenCliConfig(protected_tabs=frozenset({PAGE})))
    with pytest.raises(DriverError, match="no page is open"):
        kit.run(driver.fill("#x", "y"))
    with pytest.raises(DriverError, match="protected tab"):
        kit.run(driver.goto(URL))


def test_unavailable_bridge_is_actionable(kit: SimpleNamespace, options: BrowserOptions) -> None:
    fake = FakeOpenCli()
    fake.unavailable = True
    with pytest.raises(OpenCliUnavailable, match="opencli doctor"):
        kit.run(OpenCliSessionFactory(CONFIG, runner=fake).start(options))


def test_real_subprocess_runner_passes_arguments_literally(kit: SimpleNamespace, tmp_path: Path) -> None:
    fake_cli = tmp_path / "opencli"
    fake_cli.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps({{'page': 'P1', 'argv': sys.argv[1:]}}))\n")
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC)
    driver = OpenCliDriver(OpenCliConfig(executable=str(fake_cli)))
    tricky = "http://127.0.0.1:9/a?b=$(whoami)&c=`id`;d"
    payload = kit.run(driver._call(driver._argv(["open"], positionals=[tricky], pin=False)))
    assert payload["argv"][-1] == tricky


def test_timeout_is_unknown_not_success(kit: SimpleNamespace, tmp_path: Path) -> None:
    slow = tmp_path / "opencli"
    slow.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    driver = OpenCliDriver(OpenCliConfig(executable=str(slow), command_timeout_s=0.3))
    with pytest.raises(OpenCliTimeout) as caught:
        kit.run(driver._call(driver._argv(["tab", "list"], pin=False)))
    assert not isinstance(caught.value, NotActionable)  # an action may have happened


# --- envelopes and verification --------------------------------------------------------


def _opened(fake: FakeOpenCli, kit: SimpleNamespace) -> OpenCliDriver:
    driver = driver_with(fake)
    kit.run(driver.goto(URL))
    return driver


def test_target_errors_mean_nothing_was_done(kit: SimpleNamespace) -> None:
    driver = _opened(FakeOpenCli(), kit)
    with pytest.raises(OpenCliTargetError):
        kit.run(driver.click("#missing"))
    with pytest.raises(OpenCliTargetError):
        kit.run(driver.click("#missing", trial=True))


def test_fill_is_verified_by_envelope_value_and_document(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    for sel in ("#a", "#b", "#c"):
        fake.control(sel)
    driver = _opened(fake, kit)
    kit.run(driver.fill("#a", "Avery"))
    assert fake.controls["#a"]["value"] == "Avery"
    fake.reidentify.add("#b")
    with pytest.raises(UnverifiedAction, match="re-identified"):
        kit.run(driver.fill("#b", "Quill"))
    fake.navigate_on.add("#c")
    with pytest.raises(UnverifiedAction, match="navigated"):
        kit.run(driver.fill("#c", "x"))


def test_select_never_accepts_another_option_and_multi_is_unsupported(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    fake.control("#single")
    fake.control("#multi")
    driver = _opened(fake, kit)
    kit.run(driver.select_values("#single", ["wa_yes"]))
    fake.select_label_first["1"] = "one_label_match"  # OpenCLI matches labels before values
    with pytest.raises(UnverifiedAction):
        kit.run(driver.select_values("#single", ["1"]))
    before = len(fake.calls)
    with pytest.raises(CapabilityUnsupported, match="yourself"):
        kit.run(driver.select_values("#multi", ["a", "b"]))
    assert len(fake.calls) == before  # nothing was sent


def test_checks_are_read_back(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    fake.control("#agree")
    driver = _opened(fake, kit)
    kit.run(driver.set_checked("#agree", True))
    kit.run(driver.set_checked("#agree", False))
    assert commands(fake)[-2:] == ["uncheck", "eval"]


def test_upload_unavailable_is_actionable_and_user_attachment_is_accepted(
    kit: SimpleNamespace, tmp_path: Path
) -> None:
    resume = tmp_path / "resume_fixture.pdf"
    resume.write_bytes(b"%PDF-1.4 fictional")
    fake = FakeOpenCli()
    fake.control("#resume")
    driver = _opened(fake, kit)
    with pytest.raises(CapabilityUnsupported, match=r"Attach 'resume_fixture\.pdf'"):
        kit.run(driver.set_files("#resume", resume))
    # The person attaches the same file in the visible browser: nothing more is sent.
    fake.controls["#resume"]["files"] = [{"name": resume.name, "size": resume.stat().st_size}]
    fake.digests["#resume"] = hashlib.sha256(resume.read_bytes()).hexdigest()
    sent = len(fake.calls)
    kit.run(driver.set_files("#resume", resume))
    assert "upload" not in commands(fake)[sent:]
    fake.controls["#resume"]["files"] = []
    fake.upload_allowed = True
    kit.run(driver.set_files("#resume", resume))
    assert fake.controls["#resume"]["files"][0]["name"] == resume.name


def test_trial_click_is_read_only_and_real_click_is_structured(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    fake.control("#submit")
    driver = _opened(fake, kit)
    kit.run(driver.click("#submit", trial=True))
    assert "click" not in commands(fake)
    kit.run(driver.click("#submit"))
    assert commands(fake)[-1] == "click"


def test_evaluation_is_read_only(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    driver = _opened(fake, kit)
    sent = len(fake.calls)
    for script in ("() => document.forms[0].submit()", "(s) => { document.querySelector(s).value = 'x'; }",
                   "() => fetch('/apply', {method: 'POST'})", "() => { location = '/x'; }"):
        with pytest.raises(DriverError, match="read-only"):
            kit.run(driver.evaluate(script))
    assert len(fake.calls) == sent
    with pytest.raises(DriverError, match="fixed read-only"):
        kit.run(driver.evaluate("() => 1"))
    fake.calls.clear()

    async def garbage(argv: Sequence[str], timeout_s: float) -> CommandResult:
        return CommandResult(0, "not json at all", "")

    driver._run = garbage
    with pytest.raises(OpenCliError, match="non-JSON"):
        kit.run(driver.evaluate(oc._DOC_STATE))


def test_settle_follows_navigation_and_status(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    fake.control("#next")
    driver = _opened(fake, kit)
    kit.run(driver.click("#next"))
    fake.origin, fake.url, fake.status = "2000.25", "http://127.0.0.1:9/jobs/demo/step/2", 422
    kit.run(driver.settle(2.0))
    assert driver.url.endswith("/step/2") and driver.last_status == 422
    kit.run(driver.click("#next"))
    kit.run(driver.settle(2.0))  # no navigation: returns after the grace period
    assert driver.url.endswith("/step/2")


def test_focus_is_unsupported_not_pretended(kit: SimpleNamespace) -> None:
    driver = _opened(FakeOpenCli(), kit)
    with pytest.raises(CapabilityUnsupported, match="switch to the tab"):
        kit.run(driver.bring_to_front())


# --- the runtime over the OpenCLI driver ----------------------------------------------


def test_runtime_semantics_are_identical_over_opencli(
    kit: SimpleNamespace, server: Any, options: BrowserOptions
) -> None:
    async def capture() -> tuple[Any, Any]:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            inspection = await browser.open(server.url("/jobs/validation/apply"))
            return inspection.form, await browser.page.evaluate(inspector_script())
        finally:
            await browser.close()

    playwright_form, snapshot = kit.run(capture())
    fake = FakeOpenCli()
    fake.snapshot = snapshot
    fake.url = snapshot["url"]
    for control in snapshot["controls"]:
        fake.control(control["selector"])
    fake.native_invalid = ["Resume: Please select a file."]

    async def scenario() -> dict[str, Any]:
        out: dict[str, Any] = {}
        browser = await OpenCliSessionFactory(CONFIG, runner=fake).start(options)
        try:
            form = (await browser.open(snapshot["url"])).form
            out["form"] = form
            out["fill"] = await browser.fill(form, kit.build(form, kit.CORE).packet)
            out["action"] = await browser.submit()
            out["observation"] = await browser.confirm()
            with pytest.raises(SubmissionRefused):
                await browser.advance()
            out["location"] = browser.location
        finally:
            await browser.close()
        return out

    out = kit.run(scenario())
    assert out["form"].fingerprint == playwright_form.fingerprint
    results = {r.field_id: r for r in out["fill"].fields}
    assert results["resume"].status is FieldFillStatus.FAILED
    assert "Attach 'resume_avery_quill.pdf'" in (results["resume"].detail or "")
    assert all(r.status is FieldFillStatus.FILLED for k, r in results.items() if k != "resume")
    assert not out["fill"].ok
    assert not out["action"].dispatched
    assert out["observation"].outcome is SubmissionOutcome.NOT_SUBMITTED
    assert out["observation"].next_state is NotSubmittedNext.FILLING
    assert "click" not in commands(fake)
    assert "fixture-profile" in out["location"] and "imx-application" in out["location"]
    assert commands(fake)[-1] == "close"
    assert server.submissions()["accepted_count"] == 0


def test_ownership_precedes_every_navigation_and_sessions_never_collide(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    first, second = driver_with(fake), driver_with(fake)
    assert first.session != second.session
    assert first.session.startswith(CONFIG.session + "-")
    kit.run(first.goto(URL))
    fake.created = "SECOND_OWNED_TAB"
    fake.tabs.append(fake.created)
    kit.run(second.goto(URL))
    assert first.tab == PAGE and second.tab == "SECOND_OWNED_TAB"
    for driver in (first, second):
        calls = [c for c in fake.calls if c[c.index("browser") + 1] == driver.session]
        assert calls[0][calls[0].index("browser") + 2:] == ["tab", "new", "--window", "background"]
        assert calls[1][-2:] == ["tab", "list"]
        opened = next(c for c in calls if "open" in c)
        assert opened[opened.index("--tab") + 1] == driver.tab
    kit.run(first.release())
    assert fake.calls[-2][-2:] == ["--", PAGE]
    assert fake.calls[-1][fake.calls[-1].index("browser") + 1] == first.session
    assert second.tab == "SECOND_OWNED_TAB"


@pytest.mark.parametrize("reason", ["protected", "unlisted"])
def test_no_open_or_release_when_tab_ownership_is_unproven(kit: SimpleNamespace, reason: str) -> None:
    fake = FakeOpenCli()
    if reason == "unlisted":
        fake.tabs = []
    cfg = OpenCliConfig(protected_tabs=frozenset({PAGE}) if reason == "protected" else frozenset())
    driver = driver_with(fake, cfg)
    with pytest.raises(DriverError):
        kit.run(driver.goto(URL))
    kit.run(driver.release())
    assert "open" not in commands(fake) and "close" not in commands(fake)
    assert driver.tab is None


def test_release_can_leave_owned_final_review_tab_open(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    driver = driver_with(fake)
    kit.run(driver.goto(URL))
    count = len(fake.calls)
    kit.run(driver.release(keep_tab=True))
    after_release = fake.calls[count:]
    assert after_release == []
    assert driver.tab == PAGE
    kit.run(driver.release(keep_tab=True))
    assert fake.calls[count:] == after_release


@pytest.mark.parametrize("attached", [b"different bytes!", None])
def test_same_name_and_size_never_substitute_for_the_pinned_file_digest(
    kit: SimpleNamespace, tmp_path: Path, attached: bytes | None,
) -> None:
    path = tmp_path / "fictional_resume.pdf"
    path.write_bytes(b"approved bytes!!")
    fake = FakeOpenCli()
    fake.control("#resume")["files"] = [{"name": path.name, "size": path.stat().st_size}]
    fake.digests["#resume"] = hashlib.sha256(attached).hexdigest() if attached else None
    driver = _opened(fake, kit)
    with pytest.raises(CapabilityUnsupported, match="exact pinned file"):
        kit.run(driver.set_files("#resume", path))
    assert "upload" not in commands(fake)


def test_arbitrary_bracket_mutation_is_rejected_before_eval(kit: SimpleNamespace) -> None:
    fake = FakeOpenCli()
    driver = _opened(fake, kit)
    sent = len(fake.calls)
    for script in ("() => document.forms[0]['requestSubmit']()", "() => window['fetch']('/apply')"):
        with pytest.raises(DriverError, match="fixed read-only"):
            kit.run(driver.evaluate(script))
    assert len(fake.calls) == sent


@pytest.mark.parametrize("failure", ["readback", "missing", "bad_envelope", "url_only"])
def test_runtime_aborts_remaining_fields_and_submit_after_document_loss(
    kit: SimpleNamespace, server: Any, options: BrowserOptions, failure: str,
) -> None:
    async def capture() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            await browser.open(server.url("/jobs/validation/apply"))
            return await browser.page.evaluate(inspector_script())
        finally:
            await browser.close()

    class LostPage(FakeOpenCli):
        async def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
            result = await super().__call__(argv, timeout_s)
            if "fill" in argv and commands(self).count("fill") == 1:
                if failure == "url_only":
                    self.url = "http://localhost:9/other-origin"
                else:
                    self.origin = "9999.9"
                if failure == "missing":
                    self.controls.clear()
                if failure == "bad_envelope":
                    return self.error("selector_not_found", "target disappeared")
            return result

    fake = LostPage()
    fake.snapshot = kit.run(capture())
    for control in fake.snapshot["controls"]:
        fake.control(control["selector"])

    async def scenario() -> None:
        browser = await OpenCliSessionFactory(CONFIG, runner=fake).start(options)
        try:
            form = (await browser.open(fake.snapshot["url"])).form
            assert form is not None
            packet = kit.build(form, kit.CORE).packet
            filled = await browser.fill(form, packet)
            assert not filled.ok
            assert "page changed" in " ".join(filled.page_errors) or "navigated" in " ".join(filled.page_errors)
            assert commands(fake).count("fill") == 1
            # Uploads come first (an upload may make the site autofill other fields);
            # after the document was lost nothing else is operated.
            after_loss = commands(fake)[commands(fake).index("fill"):]
            assert not {"upload", "select", "check", "uncheck"} & set(after_loss)
            assert not (await browser.submit()).dispatched
            with pytest.raises(ValueError, match="inspect"):
                await browser.fill(form, packet)
            await browser.inspect()
            assert not (await browser.submit()).dispatched  # inspection alone is not a refill
            assert "click" not in commands(fake)
        finally:
            await browser.close()
    kit.run(scenario())
    assert server.submissions()["accepted_count"] == 0
