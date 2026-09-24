"""A fictional board at scale, for the ``GET /applications`` tests and benchmark (WP11).

``seed_board`` records applications through the canonical store's own operations, in
the shapes the runner leaves behind (``apps/cli/src/interviewmaxxing_cli/runner.py``):
three-page preparations, a preparation resumed after a question round, a second
"Prepare again", question stops, failures, sign-in stops and bare requests. It adds
pipeline cards through the pipeline store: cards linked to an application, unlinked
cards whose URL resolves to one (a tracking parameter added), and unrelated cards.
``count_statements`` counts the SQL statements run on every SQLite connection opened
inside it.

All data is fictional (``127.0.0.1:9``); nothing opens a browser or a network
connection. As a benchmark, in a temporary ``IMX_HOME``:

    uv run --no-sync python tests/service/board_support.py --applications 500
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from interviewmaxxing_core import (
    AnswerSource,
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    Claim,
    ControlType,
    EvidenceKind,
    EvidenceRef,
    IdentityEvidenceKind,
    JobIdentityObservation,
    LocalPaths,
    MissingInput,
    MissingReason,
    PacketAnswer,
    Provenance,
    SemanticType,
    TextValue,
    UserInput,
)
from interviewmaxxing_pipeline import NewPipelineItem, PipelineStore, TrackingFields

S = ApplicationState
OWNER = "fictional-board-seed"
PREPARED_REASON = "prepared for final review; submission disabled"
PAGES = 3
KINDS = (
    "prepared", "prepared", "prepared", "prepared_after_question", "prepared_twice",
    "questions", "failed", "prepared_then_asked", "failed_then_prepared", "idle",
)
"""Application shapes, cycled: six in ten end in a prepared stop."""
PREPARED_KINDS = frozenset({
    "prepared", "prepared_after_question", "prepared_twice", "failed_then_prepared",
})


def application_url(index: int) -> str:
    return f"http://127.0.0.1:9/fictional-board/{7000 + index}/apply?src=board"


def _page(index: int, step: int) -> ApplicationForm:
    fields = [
        ApplicationField(
            id=f"fld_b{step}_{n}", label=f"Fictional question {step + 1}.{n + 1}",
            semantic_type=SemanticType.CUSTOM_TEXT, control_type=ControlType.TEXT,
            selector=f"#fld_b{step}_{n}", required=True,
        )
        for n in range(4)
    ]
    final = step == PAGES - 1
    return ApplicationForm(
        url=f"http://127.0.0.1:9/fictional-board/{7000 + index}/apply/page-{step + 1}",
        ats_type="greenhouse", step=step, fields=fields, is_final_step=final,
        submit_selector="#submit" if final else None, next_selector=None if final else "#next",
    )


@dataclass
class _Run:
    """One application's claim, with the runner's store recipes."""

    store: ApplicationStore
    claim: Claim
    index: int
    noise: int = 0
    """Extra field events recorded per page, as a long runner history has."""
    shots: int = 0
    last_packet: str | None = None
    forms: dict[int, ApplicationForm] = field(default_factory=dict)

    def to(self, state: ApplicationState, **kwargs: Any) -> None:
        if self.store.get_application(self.claim.application_id).state is not state:
            self.store.transition(self.claim, state, **kwargs)

    def form(self, step: int) -> ApplicationForm:
        return self.forms.setdefault(step, _page(self.index, step))

    def save(self, step: int, missing: list[MissingInput]) -> None:
        form = self.form(step)
        app = self.store.get_application(self.claim.application_id)
        skipped = {m.field_id for m in missing}
        packet = ApplicationPacket(
            application_id=app.id, job_id=app.job_id, candidate_id=app.candidate_id,
            form_url=form.url, form_step=step, form_fingerprint=form.fingerprint,
            answers=[
                PacketAnswer(
                    field_id=f.id, semantic_type=f.semantic_type,
                    value=TextValue(text=f"Fictional answer {self.index}.{step}.{n}"),
                    provenance=Provenance(source=AnswerSource.GENERATED_FROM_FACTS,
                                          reference_ids=[f"fact_board_{n}"]),
                    confidence=0.9,
                )
                for n, f in enumerate(form.fields)
                if f.id not in skipped
            ],
            missing_inputs=missing,
        )
        self.store.save_packet(self.claim, packet)
        self.last_packet = packet.id

    def bind(self) -> None:
        self.to(S.INSPECTING)
        self.store.bind_job_identity(self.claim, JobIdentityObservation(
            ats_type="greenhouse", ats_tenant="fictional-board",
            external_job_id=str(7000 + self.index),
            evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
            evidence=f"fictional job id {7000 + self.index} in the form action",
            title=f"Fictional Marketer {self.index}", company=f"Fictional Board Co {self.index}",
        ))

    def fill(self, step: int) -> None:
        """One page: inspected, its packet, PACKET_READY, FILLING, and page events."""
        self.to(S.INSPECTING)
        self.store.append_event(self.claim, "form.discovered",
                                {"form_url": self.form(step).url, "form_step": step})
        self.save(step, [])
        self.to(S.PACKET_READY)
        self.to(S.FILLING)
        for n in range(self.noise):
            self.store.append_event(self.claim, "field.checked",
                                    {"form_step": step, "field_id": f"fld_b{step}_{n % 4}"})
        self.store.append_event(self.claim, "page.completed", {"form_step": step})

    def pages(self, start: int = 0) -> None:
        for step in range(start, PAGES):
            self.fill(step)

    def screenshot(self, name: str) -> None:
        self.shots += 1
        self.store.add_evidence(self.claim, [EvidenceRef(
            kind=EvidenceKind.SCREENSHOT,
            path=f"{self.claim.application_id}/{name}-{self.shots}.png",
            description=f"fictional {name} (screenshot)",
        )])

    def stop(self, missing: list[MissingInput], reason: str) -> None:
        self.to(S.INSPECTING)
        self.store.transition(self.claim, S.NEEDS_INPUT, metadata={
            "missing_inputs": [m.model_dump(mode="json") for m in missing], "reason": reason,
        })

    def prepare(self, *, captcha: bool = False) -> None:
        """The preparation branch on the filled final page."""
        final = self.form(PAGES - 1)
        self.screenshot("review")
        self.store.append_event(self.claim, "preparation.ready", {
            "form_url": final.url + "?draft=fictional-token", "form_step": final.step,
            "form_fingerprint": final.fingerprint, "packet_id": self.last_packet,
            "submitted": False, "browser_location": None, "captcha_pending": captcha,
        })
        self.stop([], PREPARED_REASON)

    def ask(self, step: int) -> MissingInput:
        """A page with a question only the user can answer: its packet, then the stop."""
        self.to(S.INSPECTING)
        form = self.form(step)
        missing = MissingInput.for_field(form, form.fields[0], reason=MissingReason.NO_ANSWER,
                                         prompt="Please answer the fictional question.")
        self.save(step, [missing])
        self.stop([missing], "questions need your answers")
        return missing

    def answer(self, missing: MissingInput) -> None:
        self.store.save_user_inputs(self.claim, [
            UserInput.answering(missing, TextValue(text=f"Fictional reply {self.index}")),
        ])

    def fail(self) -> None:
        self.to(S.INSPECTING)
        self.screenshot("error")
        self.store.transition(self.claim, S.FAILED_RETRYABLE,
                              failure_reason="Stopped by a fictional browser error.")

    def sign_in(self) -> None:
        self.to(S.INSPECTING)
        self.stop([MissingInput(field_id=None, label="Sign in", reason=MissingReason.USER_ACTION,
                                prompt="Sign in in the browser window, then continue.")],
                  "user action required")


def _seed_kind(run: _Run, kind: str) -> None:
    if kind == "idle":
        if run.index % 20 == 9:
            run.sign_in()
        return  # otherwise only requested
    run.bind()
    if kind == "prepared":
        run.pages()
        run.prepare(captcha=run.index % 2 == 0)
    elif kind == "prepared_after_question":
        run.fill(0)
        run.answer(run.ask(1))
        run.pages(start=1)  # the site kept the draft: page 1 is not filled again
        run.prepare()
    elif kind == "prepared_twice":
        run.pages()
        run.prepare()
        run.pages()
        run.prepare()
    elif kind == "questions":
        run.fill(0)
        run.ask(1)
    elif kind == "failed":
        run.fill(0)
        run.fail()
    elif kind == "prepared_then_asked":
        run.pages()
        run.prepare()
        run.fill(0)
        run.ask(1)
    elif kind == "failed_then_prepared":
        run.fill(0)
        run.fail()
        run.pages()
        run.prepare()
    else:  # pragma: no cover - a typo in KINDS
        raise ValueError(kind)


@dataclass(frozen=True)
class Board:
    applications: dict[str, str]
    """Application id -> kind, in creation order."""
    cards: dict[str, str | None]
    """Pipeline card id -> the application it points at (linked or by URL), if any."""


def seed_board(
    paths: LocalPaths,
    *,
    applications: int,
    cards: int | None = None,
    noise: int = 0,
    start: int = 0,
) -> Board:
    """``applications`` new applications of ``paths.candidate_id`` (cycling ``KINDS``,
    with ``noise`` extra events per filled page; ``start`` numbers their fictional jobs,
    so a second call adds to the board) and ``cards`` pipeline cards for them (default:
    one per application). Of every ten cards, five are linked to an application, four
    are unlinked with a tracking-parameter copy of an application's URL, and one is
    unrelated or has no URL."""
    paths.ensure()
    cid = paths.candidate_id
    kinds: dict[str, str] = {}
    numbers: list[tuple[str, int]] = []
    with ApplicationStore.open(paths.state_db) as store:
        for index in range(start, start + applications):
            result = store.record_request(cid, application_url(index))
            assert result.disposition.value == "NEW", "a fictional job was seeded twice"
            app = result.application
            claim = store.claim(app.id, OWNER)
            try:
                store.require_preparation_only(claim)
                _seed_kind(_Run(store, claim, index, noise), KINDS[index % len(KINDS)])
            finally:
                store.release(claim)
            kinds[app.id] = KINDS[index % len(KINDS)]
            numbers.append((app.id, index))
    points: dict[str, str | None] = {}
    with PipelineStore.from_paths(paths) as pipeline:
        for n in range(start, start + (applications if cards is None else cards)):
            slot = n % 10
            target = numbers[(n - start) % len(numbers)] if numbers else None
            linked: str | None = None
            url: str | None
            if target is not None and slot < 5:
                linked, url = target[0], application_url(target[1])
            elif target is not None and slot < 9:
                url = application_url(target[1]) + "&utm_source=board"
            else:
                url = None if n % 20 == 19 else f"http://127.0.0.1:9/fictional-elsewhere/{n}/apply"
            item = pipeline.create_item(cid, NewPipelineItem(
                lane="saved", application_id=linked, application_url=url,
                tracking=TrackingFields(company=f"Fictional Board Co {n}", role="Marketer"),
            ))
            points[item.id] = target[0] if target is not None and slot < 9 else None
    return Board(applications=kinds, cards=points)


@contextmanager
def count_statements() -> Iterator[list[str]]:
    """Every SQL statement run on a SQLite connection opened inside the block."""
    statements: list[str] = []
    connect = sqlite3.connect

    def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with mock.patch.object(sqlite3, "connect", traced):
        yield statements


# --- benchmark ---------------------------------------------------------------------------


class _NoCandidates:
    """The list never reads the candidate profile."""

    def setup(self, candidate_id: str) -> Any:
        raise AssertionError("the application list never reads the candidate")


def _service(paths: LocalPaths) -> Any:
    from interviewmaxxing_service import Dispatcher, PresentationService, ServiceConfig
    from interviewmaxxing_service.application_links import ApplicationLinks
    from interviewmaxxing_service.pipeline_api import PipelineApi

    def no_runner(_interaction: Any) -> Any:
        raise AssertionError("the application list never dispatches a run")

    config = ServiceConfig(paths=paths, allowed_origin="http://127.0.0.1:4317", port=0)
    service = PresentationService(config, candidates=_NoCandidates(), dispatcher=Dispatcher(no_runner))
    service.application_links = ApplicationLinks(
        PipelineApi(paths, paths.candidate_id), get_listing=lambda _lid: None,
        track_listing=lambda _lid: "",
    )
    return service


def measure(paths: LocalPaths, *, repeat: int) -> dict[str, Any]:
    """Time ``list_applications`` serialized as the route sends it, and count its SQL."""
    service = _service(paths)
    try:
        timings: list[float] = []
        counts: list[int] = []
        size = 0
        for _ in range(repeat):
            with count_statements() as statements:
                started = time.perf_counter()
                size = len(json.dumps(service.list_applications().dump()).encode())
                timings.append(time.perf_counter() - started)
            counts.append(len(statements))
        return {
            "first_ms": round(timings[0] * 1000, 1),
            "median_ms": round(statistics.median(timings) * 1000, 1),
            "statements": counts[0],
            "response_bytes": size,
        }
    finally:
        service.dispatcher.shutdown()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Time GET /applications on a fictional board.")
    parser.add_argument("--applications", type=int, default=500)
    parser.add_argument("--cards", type=int, default=None)
    parser.add_argument("--noise", type=int, default=0,
                        help="extra field events per filled page (a longer history)")
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="imx-board-bench-") as home:
        for name in ("IMX_PROFILE_DIR", "IMX_STATE_DB", "IMX_ARTIFACTS_DIR", "IMX_BROWSER_DIR",
                     "IMX_CANDIDATE_ID"):
            os.environ.pop(name, None)
        os.environ["IMX_HOME"] = home
        paths = LocalPaths.from_env()
        started = time.perf_counter()
        board = seed_board(paths, applications=args.applications, cards=args.cards,
                           noise=args.noise)
        seeded = time.perf_counter() - started
        with ApplicationStore.open(paths.state_db) as store:
            events = sum(len(store.list_events(app_id)) for app_id in board.applications)
        result = measure(paths, repeat=args.repeat)
        result.update(applications=len(board.applications), cards=len(board.cards),
                      events=events, seed_s=round(seeded, 1))
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
