from __future__ import annotations

import re
import subprocess
from importlib import resources
from typing import Any

import pytest

from interviewmaxxing_jobs.opencli import OpenCliTransport, TransportError, extraction_script

BANNER = "\n  Update available: v1.8.6 → v1.8.7\n  Run: npm install -g @jackwener/opencli\n"


class Runner:
    def __init__(self, *responses: tuple[int, str, str]) -> None:
        self.responses = list(responses)
        self.argv: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, list) and kwargs.get("text") and "shell" not in kwargs
        self.argv.append(argv)
        code, out, err = self.responses.pop(0) if self.responses else (0, "", BANNER)
        return subprocess.CompletedProcess(argv, code, out, err)


def test_commands_are_argument_arrays_on_the_owned_background_session() -> None:
    runner = Runner((0, '{"url": "x"}', BANNER), (0, "{}", ""), (0, '{"a": [1]}\n', BANNER))
    t = OpenCliTransport(runner=runner)
    t.open("imx-jobs-linkedin", "https://www.linkedin.com/jobs/search/?keywords=a%20b")
    t.open("imx-jobs-linkedin", "https://www.linkedin.com/jobs/view/1/")
    assert t.evaluate("imx-jobs-linkedin", "(() => '{}')()") == {"a": [1]}
    assert runner.argv[0] == ["opencli", "--profile", "jgd7jms9", "browser", "imx-jobs-linkedin",
                              "open", "https://www.linkedin.com/jobs/search/?keywords=a%20b",
                              "--window", "background"]
    assert runner.argv[1][-1] == "https://www.linkedin.com/jobs/view/1/"  # window set once
    assert runner.argv[2][5:6] == ["eval"]


@pytest.mark.parametrize("session", ["imx-assessment-opencli", "default", "imx-jobs-", "IMX-JOBS-X"])
def test_other_sessions_are_refused(session: str) -> None:
    runner = Runner()
    with pytest.raises(ValueError, match="refusing browser session"):
        OpenCliTransport(runner=runner).open(session, "https://example.test/")
    assert runner.argv == []


def test_failures_are_explained_without_the_update_banner() -> None:
    t = OpenCliTransport(runner=Runner((1, "", "✖  Error: Selector not found: #x\n" + BANNER),
                                       (1, "", "✖  Error: boom\n" + BANNER),
                                       (0, "not json", "")))
    assert t.wait_for("imx-jobs-indeed", "#x", 1000) is False
    with pytest.raises(TransportError, match="opencli eval: Error: boom"):
        t.evaluate("imx-jobs-indeed", "x")
    with pytest.raises(TransportError, match="non-JSON"):
        t.evaluate("imx-jobs-indeed", "x")


def test_click_error_envelope_raises() -> None:
    t = OpenCliTransport(runner=Runner(
        (0, '{"error": {"code": "selector_not_found", "message": "none"}}', "")))
    with pytest.raises(TransportError, match="selector_not_found"):
        t.click("imx-jobs-google", "[data-share-url]", nth=3)


def test_extraction_scripts_are_read_only() -> None:
    js = resources.files("interviewmaxxing_jobs") / "js"
    names = [p.name for p in js.iterdir() if p.name.endswith(".js")]
    assert len(names) == 9
    forbidden = re.compile(r"\.click\(|\.submit\(|dispatchEvent|\.focus\(|location\.(href|assign|replace)\s*=|"
                           r"location\.assign|location\.replace|\bfetch\(|XMLHttpRequest|\.value\s*=")
    for name in names:
        text = (js / name).read_text("utf-8")
        assert not forbidden.search(text), name
    script = extraction_script("indeed_search")
    assert script.startswith("(() => {") and script.rstrip().endswith("})()")
