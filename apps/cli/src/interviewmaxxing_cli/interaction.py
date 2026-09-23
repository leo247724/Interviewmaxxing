"""Terminal ``UserInteraction`` for ``--interactive`` runs.

Asks each required question on the terminal (skipping one leaves it for later) and,
for sign-in or CAPTCHA, asks the user to act in the visible browser window and press
Enter. Without ``--interactive`` the CLI uses ``NoninteractiveInteraction``: nothing
is asked and the run stops with a recorded NEEDS_INPUT result instead.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from typing import TextIO

from interviewmaxxing_core import AnswerReuse, MissingInput, UserInput

from .answers import AnswerError, describe, user_input_for


class TerminalInteraction:
    def __init__(self, *, reuse: AnswerReuse = AnswerReuse.APPLICATION,
                 out: TextIO | None = None) -> None:
        self.reuse = reuse
        self.out = out or sys.stderr

    async def _ask(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        line: str = await asyncio.to_thread(sys.stdin.readline)
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
