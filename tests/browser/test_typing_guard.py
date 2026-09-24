"""Drivers never type control characters key by key: a newline pressed inside a form is
the Enter key and submits or advances it outside the submission guards."""

import asyncio

import pytest

from interviewmaxxing_browser.driver import NotActionable, reject_control_characters
from interviewmaxxing_browser.opencli import OpenCliConfig, OpenCliDriver


@pytest.mark.parametrize("text", ["Austin, TX\n", "Austin\rTX", "a\tb", "x\x7f", "\x00", "\x1b[A"])
def test_reject_control_characters_raises_for_every_control_key(text):
    with pytest.raises(NotActionable, match="control character U\\+00"):
        reject_control_characters(text, "#city")


@pytest.mark.parametrize("text", ["Austin, TX", "+15125550100", "São Paulo", "O'Brien-Smith", ""])
def test_reject_control_characters_lets_ordinary_text_through(text):
    reject_control_characters(text, "#city")


class _NoRunner:
    """Fails the test if the OpenCLI driver reaches its command runner."""

    def __call__(self, *args, **kwargs):  # pragma: no cover - the guard must fire first
        raise AssertionError("the OpenCLI command runner must not be reached")


def test_opencli_type_text_refuses_before_any_command_runs():
    driver = OpenCliDriver(OpenCliConfig(profile="fixture-profile"), runner=_NoRunner())
    with pytest.raises(NotActionable, match="U\\+000A"):
        asyncio.run(driver.type_text("#city", "Austin, TX\n"))
