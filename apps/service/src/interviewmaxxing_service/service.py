"""Transport-independent presentation service: the operations behind each route.

The canonical ``ApplicationStore`` is the only state authority. This module reads it
to build views, records the user's request before dispatching work, saves answers and
user reports through the store's own operations (under a short claim), and hands all
browser work to the executor. It never transitions an application through a
submission state itself.
"""

from __future__ import annotations

import getpass
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
from typing import TypeVar
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
    SubmissionBlocked,
    SubmissionOutcome,
    SubmissionReconciliation,
    normalize_application_url,
    sha256_file,
)
from interviewmaxxing_pipeline import PipelineItem

from . import errors
from .answers import current_questions, plan_answers, unanswered_required
from .application_links import ApplicationLink, ApplicationLinks
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
    ApplicationReviewView,
    ApplicationView,
    ApprovalView,
    ApproveInput,
    BrowserActionView,
    CandidateView,
    RecheckInput,
    ReconcileInput,
    ResumeDocumentView,
    ReviewQueueView,
    ReviewStage,
    StartApplicationInput,
    SubmitInput,
    SubmitReadinessView,
    UserConfirmedNotReceivedInput,
    UserFoundConfirmationInput,
)
from .review import (
    NO_RECORDS,
    ReviewAnswers,
    ReviewInputs,
    SavedAnswerRef,
    packet_rows,
    prepared_steps,
    review_answers,
)
from .review_queue import (
    PROVIDER_EVENT,
    browser_hold,
    provider_cost,
    review_queue,
)
from .summaries import StoreSnapshot, store_snapshot, url_alias
from .views import (
    SAFE_ID,
    PriorRecord,
    Snapshot,
    application_view,
    awaited_inputs,
    form_step_of,
    iso,
    prepared_event,
    preparing_attempt,
    recorded_questions,
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
_SUBMITTED_STATES = frozenset({S.SUBMITTED, S.SUBMITTING, S.SUBMISSION_UNKNOWN})
_CLOSED_STATES = frozenset({S.DUPLICATE, S.WITHDRAWN, S.FAILED_PERMANENT})
SUBMISSION_OFF = (
    "Submission is turned off in this service. Restart it with IMX_ALLOW_SUBMISSION=1 to "
    "submit approved applications from the dashboard."
)
ANSWERED_EVENT = "input.received"


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


V = TypeVar("V")

SAVED_WORDING_LIMIT = 256
"""Applications whose saved-answer wording the service keeps (least recently used go)."""


class _LoadOnUse(Mapping[str, V]):
    """A mapping read from ``load`` the first time a value is asked for, so a view that
    needs no saved-answer wording never reads the profile. It answers lookups of one key
    only: iterating it or taking its length raises instead of reading the profile."""

    def __init__(self, load: Callable[[], Mapping[str, V]]) -> None:
        self._load = load
        self._data: Mapping[str, V] | None = None

    def _loaded(self) -> Mapping[str, V]:
        if self._data is None:
            self._data = self._load()
        return self._data

    def __getitem__(self, key: str) -> V:
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
        self._wording: OrderedDict[
            str, tuple[int, dict[str, SavedAnswerRef], frozenset[str]]
        ] = OrderedDict()
        """Application id -> (its version, the saved answers its views used, the ids looked
        up), the most recently used last; at most ``_wording_limit`` entries."""
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
        events: Sequence[ApplicationEvent] | None = None,
    ) -> ApplicationView:
        requests = store.list_requests(app.id)
        request = next((r for r in requests if r.id == app.request_id), requests[0])
        packet = store.latest_packet(app.id)
        events = store.list_events(app.id) if events is None else events
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
        """Saved-answer id -> the question it was saved for (``_saved_refs``)."""
        return {ref_id: ref.question for ref_id, ref in self._saved_refs(app, packets).items()}

    def _saved_refs(
        self, app: Application, packets: Sequence[ApplicationPacket]
    ) -> dict[str, SavedAnswerRef]:
        """``_read_saved_refs`` once per application version: the packets a view uses
        change only with the version, so repeated status reads, answers and resumes do
        not read the profile from disk again (a view that needs saved answers the kept
        entry didn't look up reads it once more). A failed read is not kept, and only the
        ``_wording_limit`` most recently used applications are."""
        wanted = frozenset(
            ref
            for packet in packets
            for answer in packet.answers
            if answer.provenance.source is AnswerSource.SAVED_ANSWER
            for ref in answer.provenance.reference_ids
        )
        with self._wording_lock:
            kept = self._wording.get(app.id)
            if kept is not None and kept[0] == app.version and wanted <= kept[2]:
                self._wording.move_to_end(app.id)
                return kept[1]
        if kept is not None and kept[0] == app.version:
            wanted |= kept[2]
        refs = self._read_saved_refs(wanted)
        if refs is None:
            return {}
        with self._wording_lock:
            self._wording[app.id] = (app.version, refs, wanted)
            self._wording.move_to_end(app.id)
            while len(self._wording) > self._wording_limit:
                self._wording.popitem(last=False)
        return refs

    def _read_saved_refs(self, wanted: frozenset[str]) -> dict[str, SavedAnswerRef] | None:
        """The wording and scope of the ``wanted`` saved answers (never their values);
        empty when none are wanted. None when the profile can't be read."""
        if not wanted or self.profile_loader is None:
            return {}
        try:
            profile = self.profile_loader()
        except Exception as exc:
            log.warning("reading saved answers for a review failed: %s", type(exc).__name__)
            return None
        if profile is None:
            return None
        return {
            saved.id: SavedAnswerRef(id=saved.id, question=saved.question, scope=saved.scope)
            for saved in profile.saved_answers
            if saved.id in wanted
        }

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
            "submission": "enabled" if self._submission_enabled else "disabled",
            "browser": "headless" if self.config.headless else "visible",
        }

    @property
    def _submission_enabled(self) -> bool:
        """``IMX_ALLOW_SUBMISSION=1`` at start, with a runner able to submit."""
        return self.config.allow_submission and self.dispatcher.can_submit

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
            location = self._card_location(link)
            if location is not None:
                # The card's location is kept on the job over a page's locality: the metro
                # rule reads it (round 5, ``record_listing``).
                store.record_listing(app.id, location=location, actor="service")
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

    def _card_location(self, link: ApplicationLink | None) -> str | None:
        """The linked card's ``locationCommute``, else its listing's location; None
        without a link or when neither states one (or they cannot be read)."""
        links = self.application_links
        if link is None or links is None:
            return None
        try:
            if link.entry_id is not None:
                with links.pipeline.store() as pipeline:
                    card = pipeline.get_item(links.pipeline.candidate_id, link.entry_id)
                if card.tracking.location_commute and card.tracking.location_commute.strip():
                    return card.tracking.location_commute.strip()
            if link.listing_id is not None:
                listing = links.get_listing(link.listing_id)
                if listing is not None and listing.location and listing.location.strip():
                    return listing.location.strip()
        except Exception as exc:
            log.warning("reading the linked card's location failed: %s", type(exc).__name__)
        return None

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
        """Save answers to the questions the application waits for and, for a prepared
        application, changes to the answers of its prepared form (the review lane's edit:
        the question ids its review lists). Nothing runs: ``resume`` prepares it again
        with them. A change that isn't valid answers 422 with the problem per question."""
        self._settle(application_id)
        with self._store() as store:
            app = self._owned(store, application_id)
            if self.dispatcher.status(app.id) is not None or app.state is not S.NEEDS_INPUT:
                raise errors.conflict("This application isn't waiting for answers right now.")
            awaited = self._awaited(store, app)
            waiting = current_questions(awaited)
            edits = self._edits(store, app, store.list_events(app.id))
            extra = [q for qid, q in edits.questions.items() if qid not in waiting]
            plan = plan_answers([*awaited, *extra], body, allowed_reuse=edits.reuse)
            if plan.stale:
                message = "These questions have changed. Reload to see the current questions."
                raise errors.conflict(message, {qid: message for qid in plan.stale})
            if plan.errors:
                if any(qid not in waiting for qid in plan.errors):
                    raise errors.invalid("Some answers need attention.", plan.errors)
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

    # --- the review lane ---------------------------------------------------------------------

    def review_queue(self) -> ReviewQueueView:
        """The Prepared queue: applications at their final review step and those held
        only by a browser step, newest first, from one read of the store."""
        with self._store(), store_snapshot(self.config.paths.state_db) as snapshot:
            items = review_queue(snapshot, self.config.candidate_id)
        return ReviewQueueView(applications=items)

    def review(self, application_id: str) -> ApplicationReviewView:
        with self._store() as store:
            app = self._owned(store, application_id)
            if app.state is S.SUBMITTING and self.dispatcher.status(app.id) is None:
                store.recover_interrupted_submissions()
                app = store.get_application(app.id)
            return self._review(store, app)

    def approve(self, application_id: str, body: ApproveInput) -> ApplicationReviewView:
        """Approve the prepared packet the person reviewed, through the store's
        ``approve_submission`` (which pins every page's packet). Submits nothing."""
        self._settle(application_id)
        with self._store() as store:
            app = self._owned(store, application_id)
            if self.dispatcher.status(app.id) is not None:
                raise errors.conflict(
                    "A run is working on this application right now. Approve it once it stops.")
            prepared = store.prepared_packet(app.id)
            if prepared is None:
                raise errors.conflict(
                    "This application isn't stopped at a completed preparation, so there is "
                    "nothing to approve. Prepare it again and review it first.")
            if body.packet_id != prepared:
                raise errors.conflict(
                    "This application was prepared again since this page was loaded. Review "
                    "the new preparation, then approve it.")
            with self._claimed(store, app.id) as claim:
                try:
                    store.approve_submission(claim, packet_id=prepared, approver=_approver())
                except SubmissionBlocked as exc:
                    raise errors.conflict(_blocked_message(exc)) from exc
            return self._review(store, store.get_application(app.id))

    def submit(self, application_id: str, body: SubmitInput) -> ApplicationReviewView:
        """Submit an approved application from the dashboard: the CLI ``submit APP --yes``
        path. Only in a service started with ``IMX_ALLOW_SUBMISSION=1``, only for the
        approval the person confirmed (``packetId``), and only where the application mode
        allows the site. Records the authorization, then runs the submission runner, which
        submits exactly the approved packets or stops before submitting."""
        if not self._submission_enabled:
            raise errors.forbidden(f"{SUBMISSION_OFF} Nothing was submitted.")
        self._settle(application_id)
        with self.dispatcher.lock, self._store() as store:
            app = self._owned(store, application_id)
            if self.dispatcher.status(app.id) is not None:
                raise errors.conflict(
                    "A run is working on this application right now. Nothing was submitted.")
            if app.state in _SUBMITTED_STATES:
                raise errors.conflict(
                    "This application was already submitted or its submission is being "
                    "settled; it is never submitted again.")
            if app.state in _CLOSED_STATES:
                raise errors.conflict("This application is closed and can't be submitted.")
            approval = store.submission_approval(app.id)
            if approval is None:
                raise errors.conflict(
                    "Approve this preparation before submitting it. Nothing was submitted.")
            if approval.packet_id != body.packet_id:
                raise errors.conflict(
                    "The approval changed since this page was loaded. Reload and check it "
                    "again. Nothing was submitted.")
            self._require_runner()
            self._require_test_site(store, app)
            if self._claim_live_elsewhere(app):
                raise errors.conflict(
                    "This application is being worked on elsewhere. Try again in a moment.")
            if self.dispatcher.busy:
                raise errors.conflict(BUSY_MESSAGE)
            with self._claimed(store, app.id) as claim:
                try:
                    store.authorize_submission(claim)
                except SubmissionBlocked as exc:
                    raise errors.conflict(_blocked_message(exc)) from exc
            before = app.state
            run = self.dispatcher.submit(
                app.id, "submit",
                lambda executor: executor.submit(app.id),
                allow_browser_action=not self.config.headless, after=self._after_run,
            )
        self._wait_for_departure(application_id, before, run)
        return self.review(application_id)

    def _review(self, store: ApplicationStore, app: Application) -> ApplicationReviewView:
        events = store.list_events(app.id)
        view = self._view(store, app, events=events)
        prepared = prepared_event(events) if app.state is S.NEEDS_INPUT else None
        stage: ReviewStage = "other"
        if prepared is not None:
            stage = "prepared"
        elif app.state is S.NEEDS_INPUT:
            stop = next((e for e in reversed(events) if e.to_state is not None), None)
            if stop is not None and browser_hold(stop.metadata) is not None:
                stage = "browser_action"
        approval = store.submission_approval(app.id)
        latest_ready = next((e for e in reversed(events) if e.event == "preparation.ready"), None)
        changed = latest_ready is not None and any(
            e.event == ANSWERED_EVENT and e.sequence > latest_ready.sequence for e in events)
        edit_note: str | None = None
        if prepared is not None:
            edits = self._edits(store, app, events, prepared=prepared)
            rows = edits.rows
            if not edits.questions and rows and all(r.no_edit_reason == NO_RECORDS for r in rows):
                edit_note = NO_RECORDS
        else:
            packet = store.latest_packet(app.id)
            rows = packet_rows([packet], self._review_inputs(store, app, events, [packet])) \
                if packet is not None and app.state not in _WORKING else []
            edit_note = "Only a prepared application's answers can be changed here."
        if self.dispatcher.status(app.id) is not None:
            edit_note = "A run is working on this application right now."
        budget = [e for e in events if e.event == PROVIDER_EVENT]
        cost = provider_cost(
            sum(float(e.metadata.get("known_cost_usd") or 0.0) for e in budget),
            sum(int(e.metadata.get("calls") or 0) for e in budget),
            sum(int(e.metadata.get("unknown_cost_calls") or 0) for e in budget),
        ) if budget else None
        captcha = latest_ready is not None and latest_ready.metadata.get("captcha_pending") is True
        return ApplicationReviewView(
            application=view,
            stage=stage,
            prepared_packet_id=store.prepared_packet(app.id),
            approval=ApprovalView(
                packet_id=approval.packet_id, approved_at=iso(approval.approved_at),
                approver=approval.approver, pages=max(len(approval.steps), 1),
            ) if approval is not None else None,
            changed_since_preparation=changed,
            provider_cost=cost,
            answers=rows,
            edit_note=edit_note,
            submit=self._submit_readiness(store, app, approved=approval is not None,
                                          changed=changed, prepared=prepared is not None,
                                          captcha=captcha),
            browser=self._browser_action(store, app),
        )

    def _review_inputs(
        self, store: ApplicationStore, app: Application, events: Sequence[ApplicationEvent],
        packets: Sequence[ApplicationPacket],
    ) -> ReviewInputs:
        return ReviewInputs(
            user_inputs=store.list_user_inputs(app.id),
            recorded=recorded_questions(events),
            saved=_LoadOnUse(lambda: self._saved_refs(app, packets)),
        )

    def _edits(
        self, store: ApplicationStore, app: Application, events: Sequence[ApplicationEvent],
        *, prepared: ApplicationEvent | None = None,
    ) -> ReviewAnswers:
        """The prepared form's rows and the questions an edit may answer; none unless
        the application is stopped at a completed preparation."""
        prepared = prepared or (prepared_event(events) if app.state is S.NEEDS_INPUT else None)
        if prepared is None:
            return ReviewAnswers(rows=[], questions={}, reuse={})
        steps = prepared_steps(events, prepared)
        packets: dict[str, ApplicationPacket] = {}
        for step in steps:
            try:
                packets[step.packet_id] = store.get_packet(step.packet_id)
            except NotFound:
                continue
        inputs = self._review_inputs(store, app, events, list(packets.values()))
        return review_answers(steps, packets, inputs)

    def _submit_readiness(
        self, store: ApplicationStore, app: Application, *, approved: bool, changed: bool,
        prepared: bool, captcha: bool,
    ) -> SubmitReadinessView:
        command = f"IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit {app.id} --yes"
        if captcha:
            command += " --act"
        problems: list[str] = []
        if not self._submission_enabled:
            problems.append(SUBMISSION_OFF)
        if app.state in _SUBMITTED_STATES:
            problems.append("This application was already submitted or its submission is "
                            "being settled; it is never submitted again.")
        elif app.state in _CLOSED_STATES:
            problems.append("This application is closed and can't be submitted.")
        elif not approved:
            if changed:
                problems.append("Answers changed since this preparation. Prepare it again, "
                                "then review and approve the new preparation.")
            elif prepared:
                problems.append("Approve this preparation first.")
            else:
                problems.append("Only an application prepared to its final review step and "
                                "approved can be submitted.")
        requests = store.list_requests(app.id)
        url = next((r.application_url for r in requests if r.id == app.request_id),
                   requests[0].application_url if requests else "")
        mode = self._mode_problem(url)
        if mode:
            problems.append("This service is in TEST_ONLY mode: it only submits to local test "
                            "sites (127.0.0.1 or localhost).")
        runner = self._runner_unavailable()
        if runner:
            problems.append(runner)
        running = self.dispatcher.status(app.id)
        if running is not None:
            problems.append("A run is working on this application right now.")
        elif self.dispatcher.busy:
            problems.append(BUSY_MESSAGE)
        elif self._claim_live_elsewhere(app):
            problems.append("This application is being worked on elsewhere. Try again in a moment.")
        if captcha and self.config.headless:
            problems.append(
                "A CAPTCHA on this form must be solved in the browser, and this service runs "
                "its browser headless. Submit it from a terminal instead.")
        return SubmitReadinessView(
            allowed=not problems, problems=problems, enabled=self._submission_enabled,
            opens_browser=not self.config.headless, command=command,
        )

    def _browser_action(self, store: ApplicationStore, app: Application) -> BrowserActionView:
        """Whether ``resume`` can open a visible browser window for this application now
        (the ``resume --act`` equivalent), and the command-line line otherwise."""
        command = f"interviewmaxxing resume {app.id} --act"
        reason: str | None = None
        if app.state in _SUBMITTED_STATES or app.state in _CLOSED_STATES:
            reason = "This application can't continue."
        elif self.config.headless:
            reason = ("This service runs its browser headless, so it can't open a window. Run "
                      "the command in a terminal instead.")
        elif self._runner_unavailable():
            reason = self._runner_unavailable()
        elif self.dispatcher.busy:
            reason = (
                "A run is working on this application right now."
                if self.dispatcher.status(app.id) is not None else BUSY_MESSAGE)
        else:
            requests = store.list_requests(app.id)
            url = next((r.application_url for r in requests if r.id == app.request_id),
                       requests[0].application_url if requests else "")
            if self._mode_problem(url):
                reason = ("This service is in TEST_ONLY mode: it only opens local test sites. "
                          "Run the command in a terminal instead.")
        return BrowserActionView(available=reason is None, reason=reason, command=command)

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


def _approver() -> str:
    """Who approved, for ``application.approved`` (the CLI records ``cli:<login>``)."""
    try:
        return f"dashboard:{getpass.getuser()}"
    except Exception:  # no login name in this environment
        return "dashboard"


_BLOCKED_MESSAGES = (
    ("answers were saved", "Answers changed since this preparation. Prepare it again so they "
                           "are filled in, then approve it."),
    ("open required questions", "Some required questions of this preparation are still open."),
    ("is missing", "A page of this preparation is missing from the store. Prepare it again."),
    ("not stopped at a completed preparation",
     "This application isn't stopped at a completed preparation. Prepare it again and review "
     "it first."),
    ("no valid approval", "Approve this preparation before submitting it."),
)


def _blocked_message(exc: SubmissionBlocked) -> str:
    """The store's refusal in plain words (its own text names ids and states)."""
    text = str(exc)
    for marker, message in _BLOCKED_MESSAGES:
        if marker in text:
            return message
    return f"The store refused it: {text}."


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
