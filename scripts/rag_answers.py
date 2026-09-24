#!/usr/bin/env python3
"""Index confirmed candidate evidence and prepare retrieval-backed local drafts.

No browser is opened and no application is submitted. JSON connection and output
files are private. `import-facts` means the user supplied and confirmed that file;
it is not an automatic resume extraction or verification command.
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
from interviewmaxxing_selection.credentials import load_api_key


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
                        connection_file: Path, max_usd: float = 1.0) -> dict[str, Any]:
    """A synthetic, local question for drafting, never a browser-ready inspection."""
    budget = CallBudget(max_calls=24, max_usd=max_usd)
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
                                             "import-facts", "draft"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--candidate-id", default=os.environ.get("IMX_CANDIDATE_ID", "default"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--connection-file", type=Path)
    parser.add_argument("--file", type=Path, help="Confirmed fact JSON or a style-only text sample")
    parser.add_argument("--listing-file", type=Path, help="Stored JobListing JSON with source provenance")
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
            facts = []
            for record in records:
                if not isinstance(record, dict) or set(record) - {"id", "key", "value", "evidence"}:
                    raise ValueError("Each fact supports id, key, value and optional evidence only")
                facts.append(CandidateFact(**record, source="user:confirmed fact import",
                    verification=FactVerification(status=VerificationStatus.VERIFIED,
                        method=VerificationMethod.USER_STATED, verified_at=utc_now())))
            store.upsert_facts(candidate.id, facts)
            print(json.dumps({"imported_facts": len(facts), "index_refresh_required": True}))
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
            if not args.listing_file:
                raise ValueError("--listing-file is required")
            listing = JobListing.model_validate(read_json(args.listing_file))
            job = job_from_listing(listing)
            if args.command == "index-job":
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
