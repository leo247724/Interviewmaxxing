#!/usr/bin/env python3
"""Index the candidate's own professional stories (a .docx) into the private RAG store
and add the atomic facts they state to the candidate profile.

The document is parsed with the standard library, split into stories at its headings,
chunked into self-contained retrieval units (one summary plus sections of about
120-300 words, each headed by the story's title, employer, role, period and themes)
and indexed under the ``story`` kind as the candidate's one current stories source.
Unchanged documents need no new embeddings. Concrete first-person sentences become
user-authored ``CandidateFact`` records with ``story:<chunk id>`` provenance, merged
into the profile through the candidate store, after which the fact projection is
re-indexed so screeners and the consistency check can use them.

It opens no browser, prepares nothing and never submits. The printed and saved receipt
carries counts, ids and hashes only; the optional facts-review file is the private,
full list for the user's review. Credentials and story text are never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from interviewmaxxing_browser.ai.knowledge_runtime import build_knowledge_store
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_candidate.files import write_json_private
from interviewmaxxing_core import CandidateFact, LocalPaths, utc_now
from interviewmaxxing_generation.knowledge.stories import (
    DEFAULT_STORY_SOURCE,
    build_story_index,
    facts_review,
    story_index_receipt,
)
from interviewmaxxing_selection.credentials import load_api_key


def _unchanged(existing: CandidateFact | None, fact: CandidateFact) -> bool:
    """The same story fact is already in the profile: keep its verification time."""
    return (existing is not None and existing.is_verified
            and (existing.key, existing.value, existing.source, list(existing.evidence))
            == (fact.key, fact.value, fact.source, list(fact.evidence)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, required=True, help="the stories .docx")
    parser.add_argument("--candidate", default=os.environ.get("IMX_CANDIDATE_ID", "default"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--env-file", type=Path, help="OpenRouter credential env file (embeddings)")
    parser.add_argument("--rag-connection-file", type=Path, help="absolute private JSON connection file")
    parser.add_argument("--source-id", default=DEFAULT_STORY_SOURCE,
                        help="stories source id; the default keeps one current document")
    parser.add_argument("--keep-others", action="store_true",
                        help="keep other stories sources instead of replacing them")
    parser.add_argument("--no-facts", action="store_true",
                        help="index chunks only; do not add facts to the profile")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse, chunk and extract; touch neither the database nor the profile")
    parser.add_argument("--receipt", type=Path, help="new private JSON receipt (counts, ids, hashes)")
    parser.add_argument("--facts-review", type=Path,
                        help="new private JSON with the full fact list for review")
    args = parser.parse_args()
    for path in (args.receipt, args.facts_review):
        if path is not None and path.exists():
            print("error: --receipt and --facts-review must name new files", file=sys.stderr)
            return 2
    if not args.dry_run and (args.env_file is None or args.rag_connection_file is None):
        print("error: --env-file and --rag-connection-file are required unless --dry-run",
              file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        index = build_story_index(args.file, verified_at=utc_now())
        receipt: dict[str, Any] = {
            "candidate_sha256": hashlib.sha256(args.candidate.encode()).hexdigest(),
            "source_id": args.source_id, "dry_run": args.dry_run, "facts_enabled": not args.no_facts,
            "file_name_sha256": hashlib.sha256(args.file.name.encode()).hexdigest(),
            **story_index_receipt(index),
        }
        if not args.dry_run:
            knowledge = build_knowledge_store(load_api_key(env_file=args.env_file),
                                              args.rag_connection_file)
            receipt["stories_index"] = knowledge.index_stories(
                args.candidate, index.chunks, version=index.file_sha256,
                source_id=args.source_id, replace_others=not args.keep_others)
            if not args.no_facts:
                store = LocalCandidateStore.from_paths(LocalPaths.from_env(home=args.home))
                profile = store.load(args.candidate)
                changed = [fact for fact in index.facts
                           if not _unchanged(profile.find_fact(fact.id), fact)]
                known = {fact.id for fact in profile.facts}
                if changed:
                    profile = store.upsert_facts(args.candidate, changed)
                receipt["profile"] = {
                    "facts_added": sum(1 for fact in changed if fact.id not in known),
                    "facts_updated": sum(1 for fact in changed if fact.id in known),
                    "facts_unchanged": len(index.facts) - len(changed),
                    "verified_facts_total": len(profile.verified_facts()),
                    "facts_total": len(profile.facts),
                }
                receipt["facts_index"] = knowledge.index_candidate(profile)
        receipt["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        if args.receipt is not None:
            args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json_private(args.receipt, receipt)
        if args.facts_review is not None:
            args.facts_review.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json_private(args.facts_review, facts_review(index, candidate_id=args.candidate))
        print(json.dumps(receipt, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        # Parser, provider and database errors may carry document text or credentials.
        print(f"error: story indexing failed ({type(exc).__name__}); nothing was submitted",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
