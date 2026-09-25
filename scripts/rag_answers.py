#!/usr/bin/env python3
"""Index confirmed candidate evidence and prepare retrieval-backed local drafts.

No browser is opened and no application is submitted. JSON connection and output
files are private. `import-facts` means the user supplied and confirmed that file;
it is not an automatic resume extraction or verification command. It lists and does not
import a fact whose own evidence contradicts its period or employer, and skips rows the
story index marked `superseded`; `remove-facts --ids a,b` removes facts from the profile by
id (then run `index-profile`).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

from interviewmaxxing_browser.ai import CallBudget, build_ai_runtime
from interviewmaxxing_browser.ai.knowledge_runtime import build_knowledge_store
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_candidate.files import read_json, write_json_private
from interviewmaxxing_core import (
    Application,
    ApplicationField,
    ApplicationForm,
    ApplicationState,
    ApplicationStore,
    CandidateFact,
    CandidateProfile,
    ControlType,
    FactVerification,
    JobListing,
    JobRecord,
    LocalPaths,
    PacketContext,
    SemanticType,
    TextValue,
    VerificationMethod,
    VerificationStatus,
    normalize_application_url,
    utc_now,
)
from interviewmaxxing_generation.knowledge.stories import fact_self_contradictions, resume_roles
from interviewmaxxing_selection.credentials import load_api_key


def job_from_store(paths: LocalPaths, *, application_id: str | None, job_id: str | None) -> JobRecord:
    """The job record the application store holds (the target employer as the application
    knows it, the same record the runner gives the writer), for an application or a job id.
    A listing's company comes from its source (a board aggregator may name another
    company); the application's job record is the target."""
    if not paths.state_db.is_file():
        raise ValueError("No application store at this home; --application-id and --job-id need one")
    with ApplicationStore.open(paths.state_db) as applications:
        if application_id:
            application = applications.get_application(application_id)
            if job_id and application.job_id != job_id:
                raise ValueError("--job-id is not the application's job")
            job_id = application.job_id
        assert job_id is not None
        return applications.get_job(job_id)


def job_from_listing(listing: JobListing) -> JobRecord:
    url = listing.application_url or listing.posting_url
    if not url:
        raise ValueError("The listing has no specific application or posting URL")
    now = utc_now()
    return JobRecord(id=listing.id, application_url=url,
                     normalized_url=normalize_application_url(url), title=listing.title,
                     company=listing.company, location=listing.location,
                     created_at=now, updated_at=now)


async def prepare_draft(*, candidate: CandidateProfile, job: JobRecord, question: str,
                        purpose: Literal["answer", "cover_letter"], env_file: Path,
                        connection_file: Path, max_usd: float = 3.0) -> dict[str, Any]:
    """A synthetic, local question for drafting, never a browser-ready inspection. The fixed
    budget covers a cover letter's whole round-6 flow: its rubric review and corrective
    rewrites, the story passages' review and up to three reviewed no-slop rewrites (limits on
    reservations, which are upper bounds; the receipt records the actual cost)."""
    budget = CallBudget(max_calls=64, max_usd=max_usd)
    router, resolver = build_ai_runtime(env_file=env_file,
        writer_model="anthropic/claude-opus-5.5", budget=budget,
        rag_connection_file=connection_file)
    now = utc_now()
    form = ApplicationForm(url=job.application_url, fields=[ApplicationField(
        id="local-draft", label=question, control_type=ControlType.TEXTAREA,
        semantic_type=(SemanticType.COVER_LETTER if purpose == "cover_letter"
                       else SemanticType.UNKNOWN),
        selector="#local-draft-only", required=True, max_length=5000,
    )])
    start = time.perf_counter()
    form = await asyncio.to_thread(router.annotate, form, document_id="local-draft-only")
    application = Application(id="local-draft-only", request_id="local-draft-only",
        job_id=job.id, candidate_id=candidate.id, state=ApplicationState.INSPECTING,
        version=1, created_at=now, updated_at=now)
    context = PacketContext(application=application, candidate=candidate, job=job, form=form)
    packet = await resolver.resolve(context)
    report = router.report_for(form)
    problems = context.problems(packet)
    ready = packet.is_complete and bool(packet.answers) and not problems
    value = packet.answers[0].value if packet.answers else None
    return {
        "mode": "local_draft", "submitted": False, "purpose": purpose,
        "question": question, "job": job.model_dump(mode="json"),
        "elapsed_seconds": time.perf_counter() - start,
        "status": "READY" if ready else "NEEDS_INPUT",
        "text": value.text if ready and isinstance(value, TextValue) else None,
        "packet": packet.model_dump(mode="json"),
        "problems": problems,
        "routing": report.model_dump(mode="json") if report else None,
        "provider": budget.metadata(),
        "retrieval": getattr(resolver, "retrieval_receipts", []),
        "narratives": getattr(resolver, "narrative_traces", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("index-profile", "index-job", "index-voice",
                                             "import-facts", "remove-facts", "draft"))
    parser.add_argument("--ids", help="remove-facts: comma-separated fact ids to remove from the profile")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--candidate-id", default=os.environ.get("IMX_CANDIDATE_ID", "default"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--connection-file", type=Path)
    parser.add_argument("--file", type=Path, help="Confirmed fact JSON or a style-only text sample")
    parser.add_argument("--listing-file", type=Path, help="Stored JobListing JSON with source provenance")
    parser.add_argument("--application-id", help="draft: take the target job record (employer, title, URL) "
                                                 "from this application, as the runner does")
    parser.add_argument("--job-id", help="draft: take the target job record from the application store")
    parser.add_argument("--question")
    parser.add_argument("--cover-letter", action="store_true")
    parser.add_argument("--output", type=Path, help="New private draft receipt JSON; text saved beside it")
    args = parser.parse_args()
    try:
        store = LocalCandidateStore.from_paths(LocalPaths.from_env(home=args.home))
        candidate = store.load(args.candidate_id)
        if args.command == "import-facts":
            if not args.file:
                raise ValueError("--file is required")
            records = read_json(args.file)
            if not isinstance(records, list) or not 1 <= len(records) <= 100:
                raise ValueError("Provide 1-100 confirmed facts as a JSON array")
            roles = resume_roles(candidate)
            facts, rejected, superseded = [], [], []
            for record in records:
                if isinstance(record, dict) and record.get("superseded") is True:
                    # A confirmed fact the story index no longer stands behind: listed for
                    # removal (remove-facts), never imported.
                    if set(record) - {"id", "superseded", "reason"} or not isinstance(record.get("id"), str):
                        raise ValueError("A superseded row supports id, superseded and reason only")
                    superseded.append(record["id"])
                    continue
                if not isinstance(record, dict) or set(record) - {"id", "key", "value", "evidence"}:
                    raise ValueError("Each fact supports id, key, value and optional evidence only")
                fact = CandidateFact(**record, source="user:confirmed fact import",
                    verification=FactVerification(status=VerificationStatus.VERIFIED,
                        method=VerificationMethod.USER_STATED, verified_at=utc_now()))
                # A fact whose own evidence dates or places it elsewhere came from a confirm
                # file written before its story's link was corrected: listed, not imported.
                if reasons := fact_self_contradictions(fact, roles):
                    rejected.append({"id": fact.id, "reasons": reasons})
                    continue
                facts.append(fact)
            if facts:
                store.upsert_facts(candidate.id, facts)
            print(json.dumps({"imported_facts": len(facts), "rejected": rejected,
                              "superseded_not_imported": superseded,
                              "index_refresh_required": bool(facts)}))
            return 0
        if args.command == "remove-facts":
            ids = [value.strip() for value in (args.ids or "").split(",") if value.strip()]
            if not 1 <= len(ids) <= 100:
                raise ValueError("--ids needs 1-100 comma-separated fact ids")
            known = {fact.id for fact in candidate.facts}
            store.remove_facts(candidate.id, ids)
            print(json.dumps({"removed_facts": sorted(set(ids) & known),
                              "unknown_ids": sorted(set(ids) - known),
                              "index_refresh_required": bool(set(ids) & known)}))
            return 0
        if args.env_file is None or args.connection_file is None:
            raise ValueError("--env-file and --connection-file are required for retrieval")
        key = load_api_key(env_file=args.env_file)
        knowledge = build_knowledge_store(key, args.connection_file)
        if args.command == "index-profile":
            result = knowledge.index_candidate(candidate)
        elif args.command == "index-voice":
            if not args.file:
                raise ValueError("--file is required for a style sample")
            text = args.file.read_text(encoding="utf-8")
            result = knowledge.index_voice(candidate.id, text,
                "voice:" + hashlib.sha256(text.encode()).hexdigest())
        else:
            from_store = args.command == "draft" and bool(args.application_id or args.job_id)
            if not args.listing_file and not from_store:
                raise ValueError("--listing-file is required" + ("" if args.command != "draft"
                                 else " (or --application-id / --job-id)"))
            listing = JobListing.model_validate(read_json(args.listing_file)) if args.listing_file else None
            job = (job_from_store(LocalPaths.from_env(home=args.home), application_id=args.application_id,
                                  job_id=args.job_id)
                   if from_store else job_from_listing(listing))  # type: ignore[arg-type]
            if args.command == "index-job":
                assert listing is not None
                if not listing.description or listing.description_completeness.value != "FULL":
                    raise ValueError("Index a full observed job description before drafting")
                result = knowledge.index_job(candidate.id, job, listing.description, listing.source_url)
            else:
                if not args.output or args.output.exists() or args.output.with_suffix(".txt").exists():
                    raise ValueError("--output must name a new private receipt path")
                question = args.question or (
                    "Write a concise cover letter connecting this job's priorities naturally to my experience."
                    if args.cover_letter else None)
                if not question:
                    raise ValueError("--question or --cover-letter is required")
                result = asyncio.run(prepare_draft(candidate=candidate, job=job, question=question,
                    purpose="cover_letter" if args.cover_letter else "answer",
                    env_file=args.env_file, connection_file=args.connection_file))
                result["job_source"] = "application_store" if from_store else "listing"
                args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_json_private(args.output, result)
                if result["text"]:
                    fd = os.open(args.output.with_suffix(".txt"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as output:
                        output.write(result["text"] + "\n")
                print(json.dumps({"status": result["status"], "receipt": str(args.output),
                                  "submitted": False, "elapsed_seconds": result["elapsed_seconds"]}))
                return 0 if result["status"] == "READY" and not result["problems"] else 2
        print(json.dumps(result, default=str))
        return 0
    except Exception as exc:
        # Provider/DB exceptions can contain credentials or raw user input.
        print(f"RAG operation failed ({type(exc).__name__}); no employer submission occurred.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
