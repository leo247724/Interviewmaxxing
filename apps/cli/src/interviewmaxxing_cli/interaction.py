"""Terminal ``UserInteraction`` for ``--interactive`` runs.

Asks each required question on the terminal (skipping one leaves it for later) and,
for sign-in or CAPTCHA, asks the user to act in the visible browser window and press
Enter. Without ``--interactive`` the CLI uses ``NoninteractiveInteraction``: nothing
is asked and the run stops with a recorded NEEDS_INPUT result instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from collections.abc import Sequence
from typing import TextIO

from interviewmaxxing_core import AnswerReuse, MissingInput, UserInput

from .answers import AnswerError, describe, user_input_for


def read_line_async(stream: TextIO) -> asyncio.Future[str]:
    """Read one line from ``stream`` on a daemon thread, delivered as a future.

    Not ``asyncio.to_thread``: that uses the loop's default executor, which
    ``asyncio.Runner.close()`` waits for, so a Ctrl-C or SIGTERM while a prompt was
    open would hang the process until the user pressed Enter. A daemon thread is
    simply abandoned; the process exits, and a late line is dropped."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def deliver(line: str | None, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(line or "")

    def read() -> None:
        result: str | None = None
        error: BaseException | None = None
        try:
            result = stream.readline()
        except BaseException as exc:  # delivered to the awaiting coroutine
            error = exc
        with contextlib.suppress(RuntimeError):  # the loop is already closed
            loop.call_soon_threadsafe(deliver, result, error)

    threading.Thread(target=read, name="interviewmaxxing-stdin", daemon=True).start()
    return future


class TerminalInteraction:
    def __init__(self, *, reuse: AnswerReuse = AnswerReuse.APPLICATION,
                 out: TextIO | None = None, stdin: TextIO | None = None) -> None:
        self.reuse = reuse
        self.out = out or sys.stderr
        self.stdin = stdin or sys.stdin

    async def _ask(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        line = await read_line_async(self.stdin)
        return line.rstrip("\n")

    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]:
        answers: list[UserInput] = []
        for item in missing:
            self.out.write("\n" + describe(item) + "\n")
            while True:
                raw = await self._ask("  your answer (empty to skip): ")
                if not raw.strip():
                    break
                try:
                    answers.append(user_input_for(item, raw, reuse=self.reuse))
                    break
                except AnswerError as exc:
                    self.out.write(f"  {exc}\n")
        return answers

    async def request_action(self, message: str) -> bool:
        self.out.write(f"\nAction needed in the browser window: {message}\n")
        raw = await self._ask("Press Enter when done, or type 'skip' to stop here: ")
        return raw.strip().lower() != "skip"

    async def progress(self, message: str) -> None:
        self.out.write(f"... {message}\n")
        self.out.flush()
