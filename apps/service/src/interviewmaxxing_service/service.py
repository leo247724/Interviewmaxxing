"""Transport-independent presentation service: the operations behind each route.

The canonical ``ApplicationStore`` is the only state authority. This module reads it
to build views, records the user's request before dispatching work, saves answers and
user reports through the store's own operations (under a short claim), and hands all
browser work to the executor. It never transitions an application through a
submission state itself.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from interviewmaxxing_core import (
    CONTRACT_VERSION,
    AnswerSource,
    Application,
    ApplicationEvent,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    CandidateProfile,
    Claim,
    ClaimUnavailable,
    EvidenceKind,
    EvidenceRef,
    InvalidApplicationUrl,
    MissingInput,
    NotFound,
    ReconciliationMethod,
    SubmissionOutcome,
    SubmissionReconciliation,
    normalize_application_url,
    sha256_file,
)
from interviewmaxxing_pipeline import PipelineItem

from . import errors
from .answers import plan_answers, unanswered_required
from .application_links import ApplicationLinks
from .candidate import (
    CandidateDataInvalid,
    CandidateGateway,
    CandidateSetupError,
    candidate_view,
    identity_from_input,
    resume_view,
)
from .config import ServiceConfig, is_loopback_host
from .executor import Dispatcher, Run
from .models import (
    PRESENTATION_VERSION,
    AnswerInput,
    ApplicationListView,
    ApplicationView,
    CandidateView,
    RecheckInput,
    ReconcileInput,
    ResumeDocumentView,
    StartApplicationInput,
    UserConfirmedNotReceivedInput,
    UserFoundConfirmationInput,
)
from .summaries import StoreSnapshot, store_snapshot, url_alias
from .views import (
    SAFE_ID,
    PriorRecord,
    Snapshot,
    application_view,
    awaited_inputs,
    form_step_of,
    prepared_event,
    preparing_attempt,
    run_packet_ids,
)

log = logging.getLogger("interviewmaxxing.service")
S = ApplicationState

RUNNABLE_STATES = frozenset(
    {S.REQUESTED, S.INSPECTING, S.PACKET_READY, S.FILLING, S.FAILED_RETRYABLE}
)
"""States in which a new run may start (nothing was dispatched to the site)."""
_INTERRUPTIBLE = frozenset({S.REQUESTED, S.INSPECTING, S.PACKET_READY, S.FILLING})
_WORKING = frozenset({S.REQUESTED, S.INSPECTING, S.PACKET_READY, S.FILLING, S.SUBMITTING})
"""States the dashboard polls; the view skips optional lookups for them."""
MAX_URL_LENGTH = 2048
MAX_NOTE_LENGTH = 2000
_FILENAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f/\\]")

BUSY_MESSAGE = (
    "Another application is using the browser right now. Wait for it to finish or "
    "stop for your input, then try again."
)


@dataclass(frozen=True)
class EvidenceFile:
    path: Path
    media_type: str
    download_name: str
    attachment: bool


_INLINE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}
_DOWNLOAD_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".mhtml": "multipart/related",
    ".pdf": "application/pdf",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
}


SAVED_WORDING_LIMIT = 256
"""Applications whose saved-answer wording the service keeps (least recently used go)."""


class _LoadOnUse(Mapping[str, str]):
    """A mapping read from ``load`` the first time a value is asked for, so a view that
    needs no saved-answer wording never reads the profile. It answers lookups of one key
    only: iterating it or taking its length raises instead of reading the profile."""

    def __init__(self, load: Callable[[], Mapping[str, str]]) -> None:
        self._load = load
        self._data: Mapping[str, str] | None = None

    def _loaded(self) -> Mapping[str, str]:
        if self._data is None:
            self._data = self._load()
        return self._data

    def __getitem__(self, key: str) -> str:
        return self._loaded()[key]

    def __iter__(self) -> Iterator[str]:
        raise TypeError("saved-answer wording is looked up one id at a time, never listed")

    def __len__(self) -> int:
        raise TypeError("saved-answer wording is looked up one id at a time, never counted")


class PresentationService:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        candidates: CandidateGateway,
        dispatcher: Dispatcher,
        runner_problem: Callable[[], str | None] | None = None,
        profile_loader: Callable[[], CandidateProfile | None] | None = None,
    ) -> None:
        self.config = config
        self.runner_problem = runner_problem
        self.candidates = candidates
        self.dispatcher = dispatcher
        self.profile_loader = profile_loader
        """The configured candidate's profile, read only for saved-answer wording in
        ``review``; optional."""
        self.owner = f"service:{os.getpid()}"
        self.application_links: ApplicationLinks | None = None
        self._wording: OrderedDict[str, tuple[int, dict[str, str]]] = OrderedDict()
        """Application id -> (its version, the saved-answer wording its review used), the
        most recently used last; at most ``_wording_limit`` entries."""
        self._wording_limit = SAVED_WORDING_LIMIT
        self._wording_lock = threading.Lock()

    # --- plumbing -------------------------------------------------------------------

    @contextmanager
    def _store(self) -> Iterator[ApplicationStore]:
        """A store connection for the calling thread only."""
        store = ApplicationStore.open(self.config.paths.state_db)
        try:
            yield store
        finally:
            store.close()

    @contextmanager
    def _claimed(self, store: ApplicationStore, application_id: str) -> Iterator[Claim]:
        try:
            claim = store.claim(application_id, self.owner)
        except ClaimUnavailable as exc:
            raise errors.conflict(
                "This application is being worked on right now. Try again in a moment."
            ) from exc
        try:
            yield claim
        finally:
            store.release(claim)

    def _owned(self, store: ApplicationStore, application_id: str) -> Application:
        if not SAFE_ID.match(application_id):
            raise errors.invalid("That is not an application id.")
        try:
            app = store.get_application(application_id)
        except NotFound as exc:
            raise errors.not_found("No application with that id.") from exc
        if app.candidate_id != self.config.candidate_id:
            raise errors.not_found("No application with that id.")
        return app

    def _view(
        self,
        store: ApplicationStore,
        app: Application,
        *,
        answer_errors: dict[str, str] | None = None,
    ) -> ApplicationView:
        requests = store.list_requests(app.id)
        request = next((r for r in requests if r.id == app.request_id), requests[0])
        packet = store.latest_packet(app.id)
        events = store.list_events(app.id)
        review_packets = self._review_packets(store, app, events)
        pinned = store.pinned_resume(app.id)
        prior = None
        if app.state is S.DUPLICATE and app.duplicate_of:
            try:
                survivor = store.get_application(app.duplicate_of)
                prior = PriorRecord(
                    application=survivor,
                    application_url=store.list_requests(survivor.id)[0].application_url,
                    receipt=store.get_receipt(survivor.id),
                )
            except (NotFound, IndexError):
                prior = None
        snap = Snapshot(
            application=app,
            job=store.get_job(app.job_id),
            request=request,
            events=events,
            attempts=store.list_attempts(app.id),
            evidence=store.list_evidence(app.id),
            receipt=store.get_receipt(app.id),
            packet=packet,
            user_inputs=store.list_user_inputs(app.id),
            prior=prior,
            pinned_resume_name=pinned.filename if pinned else None,
            fallback_resume_name=(
                self._current_resume_name() if packet is None and pinned is None else None
            ),
            running=self.dispatcher.status(app.id),
            answer_errors=answer_errors or {},
            review_packets=review_packets,
            saved_questions=(
                {} if app.state in _WORKING
                else _LoadOnUse(lambda: self._saved_wording(
                    app, [*review_packets, *([packet] if packet else [])]
                ))
            ),
        )
        return application_view(snap, public_base=self.config.public_base)

    @staticmethod
    def _review_packets(
        store: ApplicationStore, app: Application, events: Sequence[ApplicationEvent]
    ) -> list[ApplicationPacket]:
        """For a prepared application, the latest packet of each step of the preparing
        attempt, so the review covers every page and not only the final one: pages
        filled before a question round included, a failed run's pages not."""
        prepared = prepared_event(events) if app.state is S.NEEDS_INPUT else None
        if prepared is None:
            return []
        packets = []
        attempt = preparing_attempt(events, prepared)
        for packet_id in run_packet_ids(attempt, last_step=form_step_of(prepared)):
            try:
                packets.append(store.get_packet(packet_id))
            except NotFound:
                continue
        return packets

    def _saved_wording(self, app: Application, packets: list[ApplicationPacket]) -> dict[str, str]:
        """``_saved_questions`` once per application version: the packets a view uses
        change only with the version, so repeated status reads, answers and resumes do
        not read the profile from disk again. A failed read is not kept, and only the
        ``_wording_limit`` most recently used applications are."""
        with self._wording_lock:
            kept = self._wording.get(app.id)
            if kept is not None and kept[0] == app.version:
                self._wording.move_to_end(app.id)
                return kept[1]
        wording = self._saved_questions(packets)
        if wording is None:
            return {}
        with self._wording_lock:
            self._wording[app.id] = (app.version, wording)
            self._wording.move_to_end(app.id)
            while len(self._wording) > self._wording_limit:
                self._wording.popitem(last=False)
        return wording

    def _saved_questions(self, packets: list[ApplicationPacket]) -> dict[str, str] | None:
        """Saved-answer id -> the question it was saved for, for the saved answers these
        packets used; empty when none were used. None when the profile can't be read."""
        wanted = {
            ref
            for packet in packets
            for answer in packet.answers
            if answer.provenance.source is AnswerSource.SAVED_ANSWER
            for ref in answer.provenance.reference_ids
        }
        if not wanted or self.profile_loader is None:
            return {}
        try:
            profile = self.profile_loader()
        except Exception as exc:
            log.warning("reading saved answers for a review failed: %s", type(exc).__name__)
            return None
        if profile is None:
            return None
        return {saved.id: saved.question for saved in profile.saved_answers if saved.id in wanted}

    @staticmethod
    def _awaited(store: ApplicationStore, app: Application) -> list[MissingInput]:
        return awaited_inputs(store.list_events(app.id), store.latest_packet(app.id), app.state)

    def _current_resume_name(self) -> str | None:
        try:
            state = self.candidates.setup(self.config.candidate_id)
        except Exception:
            return None
        selected = next((r for r in state.resumes if r.id == state.selected_resume_id), None)
        return selected.filename if selected else None

    def _settle(self, application_id: str, timeout: float = 5.0) -> None:
        """Let a run for this application that has stopped for the user finish its
        bookkeeping (and release its claim) before acting on the state it left."""
        run = self.dispatcher.current
        if run is not None and run.application_id == application_id:
            run.done.wait(timeout)

    def _claim_live_elsewhere(self, app: Application) -> bool:
        return (
            app.claim_owner is not None
            and app.claim_expires_at is not None
            and app.claim_expires_at > datetime.now(UTC)
        )

    # --- lifecycle --------------------------------------------------------------------

    def recover(self) -> int:
        """Mark submissions interrupted by a previous process as SUBMISSION_UNKNOWN."""
        with self._store() as store:
            return len(store.recover_interrupted_submissions())

    def health(self) -> dict[str, object]:
        return {
            "status": "ok",
            "service": "interviewmaxxing-service",
            "contractVersion": CONTRACT_VERSION,
            "executor": "busy" if self.dispatcher.busy else "idle",
            "runner": "unavailable" if self._runner_unavailable() else "available",
            "applicationMode": self.config.application_mode,
            "presentationVersion": PRESENTATION_VERSION,
        }

    def _runner_unavailable(self) -> str | None:
        return self.runner_problem() if self.runner_problem is not None else None

    def _mode_problem(self, url: str) -> str | None:
        """In TEST_ONLY mode the browser may only be pointed at a loopback test site."""
        if self.config.application_mode != "TEST_ONLY":
            return None
        host = urlsplit(url).hostname or ""
        if is_loopback_host(host):
            return None
        return (
            "This service is in TEST_ONLY mode: it only applies to local test sites "
            "(127.0.0.1 or localhost). Nothing was recorded or sent."
        )

    def _require_test_site(self, store: ApplicationStore, app: Application) -> None:
        requests = store.list_requests(app.id)
        url = next((r.application_url for r in requests if r.id == app.request_id),
                   requests[0].application_url if requests else "")
        problem = self._mode_problem(url)
        if problem:
            raise errors.forbidden(problem.replace("Nothing was recorded or sent.",
                                                   "Nothing was sent."))

    def _require_runner(self) -> None:
        """Refuse before recording anything when the I1 runner cannot run."""
        problem = self._runner_unavailable()
        if problem:
            raise errors.unavailable(problem)

    # --- candidate --------------------------------------------------------------------

    def get_candidate(self) -> CandidateView:
        return candidate_view(self.candidates.setup(self.config.candidate_id))

    def upload_resume(self, filename: str, content: bytes) -> ResumeDocumentView:
        name = filename.strip()
        if (
            not name
            or len(name) > 200
            or _FILENAME_FORBIDDEN.search(name)
            or name in (".", "..")
            or name.startswith(".")
        ):
            raise errors.invalid(
                "The file name isn't usable.", {"resumeFile": "Rename the file and upload it again."}
            )
        if not content:
            raise errors.invalid("The file is empty.", {"resumeFile": "The file is empty."})
        if len(content) > self.config.max_upload_bytes:
            raise errors.ApiError(
                413, "invalid", "The file is too large.",
                {"resumeFile": f"Upload a file under {self.config.max_upload_bytes // (1024 * 1024)} MB."},
            )
        try:
            entry = self.candidates.store_resume(
                self.config.candidate_id, filename=name, content=content
            )
        except CandidateSetupError as exc:
            raise errors.invalid(exc.message, {exc.field: exc.message}) from exc
        return resume_view(entry)

    # --- applications ------------------------------------------------------------------

    def start(self, body: StartApplicationInput) -> tuple[ApplicationView, bool]:
        cid = self.config.candidate_id
        url = body.application_url.strip()
        self._require_runner()
        url_error = _url_problem(url) or self._mode_problem(url)
        if url_error:
            raise errors.invalid(url_error, {"applicationUrl": url_error})
        state = self.candidates.setup(cid)
        if body.resume_id not in {r.id for r in state.resumes}:
            message = "Choose one of your saved resumes or upload one."
            raise errors.invalid(message, {"resumeId": message})
        try:
            identity = identity_from_input(
                body.profile, current=state.identity, now=datetime.now(UTC)
            )
        except CandidateSetupError as exc:
            raise errors.invalid(exc.message, {exc.field: exc.message}) from exc
        needs_upsert = (
            not state.complete
            or identity is not state.identity
            or state.selected_resume_id != body.resume_id
        )

        with self.dispatcher.lock, self._store() as store:
            if self.application_links is None and (
                body.pipeline_entry_id is not None or body.listing_id is not None
            ):
                raise errors.unavailable("Pipeline application linking isn't available.")
            link = self.application_links.prepare(body, store) if self.application_links else None
            running = self.dispatcher.current
            existing = store.find_application(cid, url)
            if (
                running is not None
                and (existing is None or existing.id != running.application_id)
                and (existing is None or existing.state in RUNNABLE_STATES)
            ):
                raise errors.conflict(BUSY_MESSAGE)
            result = store.record_request(cid, url)  # durable before any work starts
            app = result.application
            created = result.disposition.value == "NEW"
            dispatch = (
                app.state in RUNNABLE_STATES
                and (running is None or running.application_id != app.id)
                and not self._claim_live_elsewhere(app)
            )
            if dispatch:
                try:
                    if needs_upsert:
                        selected = self.candidates.upsert_profile(
                            cid, identity=identity, resume_id=body.resume_id
                        ).resume
                    else:
                        selected = self.candidates.resume_artifact(cid, body.resume_id)
                except CandidateSetupError as exc:
                    raise errors.invalid(exc.message, {exc.field: exc.message}) from exc
                except CandidateDataInvalid as exc:
                    raise errors.conflict(
                        "Your saved profile can't be read, so it wasn't changed. Check "
                        "profile.json in your Interviewmaxxing profile folder."
                    ) from exc
                # Pin exactly the resume the user selected for this application, before
                # any run. First writer wins: a repeated request or a restart keeps the
                # original pin, whatever the profile says later.
                store.pin_resume(app.id, selected)
            if link is not None and self.application_links is not None:
                self.application_links.pin_identity(link, app.id, store)
                self.application_links.bind(link, app.id, store)
            if dispatch:
                self.dispatcher.submit(
                    app.id,
                    "apply",
                    lambda executor: executor.apply(url, candidate_id=cid),
                    after=self._after_run,
                )
            return self._view(store, store.get_application(app.id)), created

    def list_applications(self) -> ApplicationListView:
        """Every application of the configured candidate, most recently updated first,
        with its preparation state and the pipeline cards that point at it. One read of
        the store with a fixed number of queries (``summaries``), however many
        applications and cards there are."""
        cid = self.config.candidate_id
        items = self._pipeline_items()
        with self._store(), store_snapshot(self.config.paths.state_db) as snapshot:
            cards = self._cards_by_application(snapshot, items)
            rows = snapshot.summaries(cid, public_base=self.config.public_base, cards=cards)
        rows.sort(key=lambda row: row.updated_at, reverse=True)
        return ApplicationListView(applications=rows)

    def _pipeline_items(self) -> list[PipelineItem]:
        links = self.application_links
        if links is None:
            return []
        try:
            with links.pipeline.store() as pipeline:
                return pipeline.list_items(links.pipeline.candidate_id)
        except Exception as exc:
            log.warning("reading pipeline cards for the application list failed: %s",
                        type(exc).__name__)
            return []

    def _cards_by_application(
        self, snapshot: StoreSnapshot, items: Sequence[PipelineItem]
    ) -> dict[str, list[str]]:
        """Pipeline card ids per application: cards linked to it, and unlinked cards whose
        application URL the store resolves to it (its own normalization and aliases), in
        one alias query for all unlinked cards."""
        aliases: dict[str, str] = {}
        for item in items:
            if item.application_id is None and item.application_url:
                try:
                    aliases[item.id] = url_alias(item.application_url)
                except (InvalidApplicationUrl, ValueError):
                    continue
        found = snapshot.applications_by_alias(self.config.candidate_id, set(aliases.values()))
        out: dict[str, list[str]] = {}
        for item in items:
            app_id = item.application_id
            if app_id is None and item.id in aliases:
                app_id = found.get(aliases[item.id])
            if app_id is not None:
                out.setdefault(app_id, []).append(item.id)
        return out

    def status(self, application_id: str) -> ApplicationView:
        with self._store() as store:
            app = self._owned(store, application_id)
            if app.state is S.SUBMITTING and self.dispatcher.status(app.id) is None:
                # A submit whose owner is gone becomes SUBMISSION_UNKNOWN once its lease
                # lapses; never a retry.
                store.recover_interrupted_submissions()
                app = store.get_application(app.id)
            return self._view(store, app)

    def answer(self, application_id: str, body: AnswerInput) -> ApplicationView:
        self._settle(application_id)
        with self._store() as store:
            app = self._owned(store, application_id)
            if self.dispatcher.status(app.id) is not None or app.state is not S.NEEDS_INPUT:
                raise errors.conflict("This application isn't waiting for answers right now.")
            plan = plan_answers(self._awaited(store, app), body)
            if plan.stale:
                message = "These questions have changed. Reload to see the current questions."
                raise errors.conflict(message, {qid: message for qid in plan.stale})
            if plan.errors:
                return self._view(store, app, answer_errors=plan.errors)
            if plan.inputs:
                job = store.get_job(app.job_id)
                with self._claimed(store, app.id) as claim:
                    store.save_user_inputs(claim, plan.inputs)
                for item in plan.inputs:
                    saved = item.to_saved_answer(job=job)
                    if saved is not None:
                        try:
                            self.candidates.save_answer(self.config.candidate_id, saved)
                        except Exception as exc:
                            # The answer is still stored for this application.
                            log.warning("saving an answer for reuse failed: %s", type(exc).__name__)
            return self._view(store, store.get_application(app.id))

    def resume(self, application_id: str) -> ApplicationView:
        self._settle(application_id)
        with self.dispatcher.lock, self._store() as store:
            app = self._owned(store, application_id)
            if self.dispatcher.status(app.id) is not None:
                return self._view(store, app)
            if app.state in (S.SUBMITTING, S.SUBMISSION_UNKNOWN):
                raise errors.conflict(
                    "The earlier submission hasn't been settled. Continuing could apply "
                    "twice, so it stays locked until the outcome is checked."
                )
            if app.state is S.SUBMITTED:
                raise errors.conflict("This application was already submitted.")
            if app.state in (S.DUPLICATE, S.WITHDRAWN, S.FAILED_PERMANENT):
                raise errors.conflict("This application is closed and can't continue.")
            if app.state is S.NEEDS_INPUT:
                missing = unanswered_required(
                    self._awaited(store, app), store.list_user_inputs(app.id)
                )
                if missing:
                    raise errors.invalid("Some required questions still need answers.", missing)
            if self._claim_live_elsewhere(app):
                raise errors.conflict(
                    "This application is being worked on elsewhere. Try again in a moment."
                )
            if self.dispatcher.busy:
                raise errors.conflict(BUSY_MESSAGE)
            self._require_runner()
            self._require_test_site(store, app)
            before = app.state
            if before is S.REQUESTED:
                url = store.list_requests(app.id)[0].application_url
                cid = self.config.candidate_id
                run = self.dispatcher.submit(
                    app.id, "apply",
                    lambda executor: executor.apply(url, candidate_id=cid),
                    allow_browser_action=True, after=self._after_run,
                )
            else:
                run = self.dispatcher.submit(
                    app.id, "resume",
                    lambda executor: executor.resume(app.id),
                    allow_browser_action=True, after=self._after_run,
                )
        self._wait_for_departure(application_id, before, run)
        return self.status(application_id)

    def _wait_for_departure(self, application_id: str, state: ApplicationState, run: Run) -> None:
        """Briefly wait until the run has moved the application on, so the response
        does not show the state the user just acted on."""
        deadline = time.monotonic() + 5.0
        with self._store() as store:
            while time.monotonic() < deadline and not run.done.is_set():
                if store.get_application(application_id).state is not state:
                    return
                time.sleep(0.05)

    def reconcile(self, application_id: str, body: ReconcileInput) -> ApplicationView:
        with self._store() as store:
            app = self._owned(store, application_id)
            if app.state is not S.SUBMISSION_UNKNOWN:
                raise errors.conflict("Only an unconfirmed submission can be settled.")
            running = self.dispatcher.status(app.id)
            if running is not None and running.kind != "reconcile":
                self._settle(app.id)
                running = self.dispatcher.status(app.id)
            if isinstance(body, RecheckInput):
                if running is None:
                    self._require_runner()
                    self._require_test_site(store, app)
                with self.dispatcher.lock:
                    current = self.dispatcher.current
                    if current is not None and current.application_id == app.id:
                        run = current
                    elif current is not None:
                        raise errors.conflict(BUSY_MESSAGE)
                    else:
                        run = self.dispatcher.submit(
                            app.id, "reconcile",
                            lambda executor: executor.reconcile(app.id),
                            after=self._after_run,
                        )
                run.done.wait(self.config.reconcile_wait_s)
                return self._view(store, store.get_application(app.id))
            if running is not None:
                raise errors.conflict("A check of the site is running. Wait for it to finish.")
            if isinstance(body, UserFoundConfirmationInput):
                self._record_user_confirmation(store, app, body)
            elif isinstance(body, UserConfirmedNotReceivedInput):
                with self._claimed(store, app.id) as claim:
                    store.add_evidence(claim, [EvidenceRef(
                        kind=EvidenceKind.USER_STATEMENT,
                        description=(
                            "You reported that no confirmation arrived and the employer has "
                            "no record of this application."
                        ),
                    )])
                    store.append_event(claim, "reconcile.user_reported_not_received", {})
            return self._view(store, store.get_application(app.id))

    def _record_user_confirmation(
        self, store: ApplicationStore, app: Application, body: UserFoundConfirmationInput
    ) -> None:
        reference = (body.reference or "").strip()[:200] or None
        note = (body.note or "").strip()[:MAX_NOTE_LENGTH] or None
        where = {
            "email": "a confirmation email",
            "portal": "the applicant portal",
            "other": "another source",
        }[body.found_in]
        detail = f"You reported a confirmation in {where}."
        if reference:
            detail += f" Reference: {reference}."
        statement = detail + (f" Note: {note}" if note else "")
        reconciliation = SubmissionReconciliation(
            outcome=SubmissionOutcome.ACCEPTED,
            method=ReconciliationMethod.USER_CONFIRMED,
            detail=detail,
            confirmation_reference=reference,
            evidence=[EvidenceRef(kind=EvidenceKind.USER_STATEMENT, description=statement)],
        )
        with self._claimed(store, app.id) as claim:
            store.reconcile_submission(claim, reconciliation)

    # --- after a run (executor thread) ------------------------------------------------------

    def _after_run(self, run: Run) -> None:
        with self._store() as store:
            app = store.get_application(run.application_id)
            if run.error is not None and app.state in _INTERRUPTIBLE:
                try:
                    claim = store.claim(app.id, self.owner)
                except ClaimUnavailable:
                    return  # the runner still holds it; a later run can take over
                try:
                    store.transition(
                        claim, S.FAILED_RETRYABLE,
                        failure_reason=(
                            "the run stopped unexpectedly before submitting; nothing was sent"
                        ),
                        metadata={"error": type(run.error).__name__},
                    )
                finally:
                    store.release(claim)
            elif (
                run.kind == "reconcile"
                and app.state is S.SUBMISSION_UNKNOWN
                and not any(
                    e.event == "reconcile.unconfirmed" and e.timestamp >= run.started_at
                    for e in store.list_events(app.id)
                )
            ):  # the runner did not record its own inconclusive check
                detail = (
                    "The check couldn't be completed. Applying again stays locked."
                    if run.error is not None
                    else "No confirmation or application record was found yet. Applying "
                    "again stays locked."
                )
                try:
                    claim = store.claim(app.id, self.owner)
                except ClaimUnavailable:
                    return
                try:
                    store.append_event(claim, "reconcile.checked", {"detail": detail})
                finally:
                    store.release(claim)

    # --- evidence ------------------------------------------------------------------------

    def evidence(self, application_id: str, evidence_id: str) -> EvidenceFile:
        if not SAFE_ID.match(evidence_id):
            raise errors.invalid("That is not an evidence id.")
        with self._store() as store:
            app = self._owned(store, application_id)
            ref = next((e for e in store.list_evidence(app.id) if e.id == evidence_id), None)
        if ref is None or ref.path is None:
            raise errors.not_found("No such evidence for this application.")
        root = self.config.paths.artifacts_dir.resolve()
        app_dir = (root / app.id).resolve()
        try:
            target = (root / ref.path).resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise errors.not_found("The evidence file is no longer on this computer.") from exc
        if not target.is_relative_to(app_dir) or not target.is_file():
            raise errors.not_found("No such evidence for this application.")
        if ref.sha256 and sha256_file(target) != ref.sha256:
            raise errors.conflict("The saved evidence file was changed after it was recorded.")
        suffix = target.suffix.lower()
        if suffix in _INLINE_TYPES:
            media, attachment = _INLINE_TYPES[suffix], False
        else:
            media, attachment = _DOWNLOAD_TYPES.get(suffix, "application/octet-stream"), True
        return EvidenceFile(
            path=target, media_type=media, download_name=f"{ref.id}{suffix}", attachment=attachment
        )


def _url_problem(url: str) -> str | None:
    if not url:
        return "Paste the application link."
    if len(url) > MAX_URL_LENGTH:
        return "That link is too long."
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Use the full link, starting with https://."
    if parts.username or parts.password:
        return "Use the link without a user name or password in it."
    try:
        normalize_application_url(url)
    except InvalidApplicationUrl:
        return "That link can't be used for an application."
    return None
