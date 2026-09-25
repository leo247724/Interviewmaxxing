#!/usr/bin/env python3
"""Index the candidate's own professional stories (a .docx) into the private RAG store
and add the atomic facts they state, and the years of experience the resume timeline
implies, to the candidate profile.

The document is parsed with the standard library, split into stories at its headings and
analysed. Each story is then linked to a resume role: by a distinctive company name the
story mentions, or else by one Jev decision over the resume roles gated at confidence 0.90
and probability 0.95. A linked story's chunks and facts carry the resume role's dates; an
unlinked story's carry a year only when the story states one; never a default year. The
chunks (one summary plus sections of about 120-300 words, each headed by the story's
title, employer, resume role, period and themes) are indexed under the ``story`` kind as
the candidate's one current stories source; unchanged documents need no new embeddings.
Concrete first-person sentences become user-authored ``CandidateFact`` records with
``story:<chunk id>`` provenance. Years of experience (total and per area) are derived from
the dated resume roles with provenance ``derived:experience_timeline``. Facts of either
provenance that this run does not produce again are removed from the profile, the new ones
are merged through the candidate store, and the fact projection is re-indexed.

It opens no browser, prepares nothing and never submits. The printed and saved receipt
carries counts, ids, hashes and link scores only; the optional facts-review files (JSON and
Markdown) are the private, full list for the user's review. Credentials and story text are
never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interviewmaxxing_browser.ai.knowledge_runtime import build_knowledge_store
from interviewmaxxing_browser.ai.providers import BoundedDecisions
from interviewmaxxing_browser.ai.stories import link_story_to_role
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_candidate.files import write_json_private
from interviewmaxxing_core import CandidateFact, CandidateProfile, LocalPaths, utc_now
from interviewmaxxing_generation.knowledge.stories import (
    DEFAULT_STORY_SOURCE,
    StoryRoleLink,
    build_story_index,
    facts_review,
    facts_review_markdown,
    match_role_by_name,
    read_stories,
    resume_roles,
    story_index_receipt,
)
from interviewmaxxing_generation.knowledge.timeline import DERIVED_SOURCE, derive_experience_years
from interviewmaxxing_selection.credentials import load_api_key
from interviewmaxxing_selection.jev import JevClient


def _unchanged(existing: CandidateFact | None, fact: CandidateFact) -> bool:
    """The same fact is already in the profile: keep its verification time."""
    return (existing is not None and existing.is_verified
            and (existing.key, existing.value, existing.source, list(existing.evidence))
            == (fact.key, fact.value, fact.source, list(fact.evidence)))


def _replaced_provenance(fact: CandidateFact) -> bool:
    return fact.source.startswith("story:") or fact.source == DERIVED_SOURCE


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, required=True, help="the stories .docx")
    parser.add_argument("--candidate", default=os.environ.get("IMX_CANDIDATE_ID", "default"))
    parser.add_argument("--home", type=Path)
    parser.add_argument("--env-file", type=Path, help="OpenRouter credential env file (embeddings, Jev)")
    parser.add_argument("--rag-connection-file", type=Path, help="absolute private JSON connection file")
    parser.add_argument("--source-id", default=DEFAULT_STORY_SOURCE,
                        help="stories source id; the default keeps one current document")
    parser.add_argument("--keep-others", action="store_true",
                        help="keep other stories sources instead of replacing them")
    parser.add_argument("--no-facts", action="store_true",
                        help="index chunks only; do not change the profile's facts")
    parser.add_argument("--no-links", action="store_true",
                        help="link stories to resume roles by company name only (no Jev decision)")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse, link by name, chunk and extract; touch neither the database "
                             "nor the profile and call no provider")
    parser.add_argument("--receipt", type=Path, help="new private JSON receipt (counts, ids, hashes)")
    parser.add_argument("--facts-review", type=Path,
                        help="new private JSON with the full fact list for review (a .md is written beside it)")
    args = parser.parse_args()
    review_markdown = args.facts_review.with_suffix(".md") if args.facts_review else None
    for path in (args.receipt, args.facts_review, review_markdown):
        if path is not None and path.exists():
            print("error: --receipt and --facts-review must name new files", file=sys.stderr)
            return 2
    if not args.dry_run and (args.env_file is None or args.rag_connection_file is None):
        print("error: --env-file and --rag-connection-file are required unless --dry-run",
              file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        now = utc_now()
        document = read_stories(args.file)
        store = LocalCandidateStore.from_paths(LocalPaths.from_env(home=args.home))
        profile: CandidateProfile | None = None
        roles = []
        try:
            profile = store.load(args.candidate)
            roles = resume_roles(profile)
        except Exception:
            if not args.dry_run:
                raise
        decisions = None
        if not args.dry_run and not args.no_links and roles:
            key = load_api_key(env_file=args.env_file)
            decisions = BoundedDecisions(JevClient(key, timeout_seconds=15, max_attempts=1))
        links: dict[str, StoryRoleLink] = {}
        link_traces: list[dict[str, Any]] = []
        for story, analysis in zip(document.stories, document.analyses, strict=True):
            link = match_role_by_name(story, analysis, roles)
            if link is None and decisions is not None:
                link, trace = link_story_to_role(story=story, analysis=analysis, roles=roles,
                                                 decide=decisions.decide, model=decisions.model)
                link_traces.append(trace)
            elif link is not None:
                link_traces.append({"stage": "story_role_link", "story_id": analysis.story_id,
                                    "status": "LINKED", "method": "employer_name",
                                    "resume_role_id": link.resume_role_id})
            if link is not None:
                links[analysis.story_id] = link
        index = build_story_index(document, verified_at=now, links=links)
        story_areas = {analysis.story_id: [*analysis.tools, *analysis.skills]
                       for analysis in document.analyses}
        derived = (derive_experience_years(profile, today=datetime.now(UTC).date(), verified_at=now,
                                           story_links=links, story_areas=story_areas)
                   if profile is not None else [])
        receipt: dict[str, Any] = {
            "candidate_sha256": hashlib.sha256(args.candidate.encode()).hexdigest(),
            "source_id": args.source_id, "dry_run": args.dry_run, "facts_enabled": not args.no_facts,
            "file_name_sha256": hashlib.sha256(args.file.name.encode()).hexdigest(),
            "resume_roles": len(roles), "link_decisions": link_traces,
            "jev_links": ("skipped: dry run" if args.dry_run else "skipped: --no-links" if args.no_links
                          else "skipped: no resume roles" if not roles else "asked"),
            **story_index_receipt(index),
            "derived_years_facts": {"count": len(derived), "keys": [fact.key for fact in derived],
                                    "values": {fact.key: fact.value for fact in derived}},
        }
        if decisions is not None:
            receipt["provider"] = decisions.budget.metadata()
        if not args.dry_run:
            knowledge = build_knowledge_store(load_api_key(env_file=args.env_file),
                                              args.rag_connection_file)
            receipt["stories_index"] = knowledge.index_stories(
                args.candidate, index.chunks, version=index.file_sha256,
                source_id=args.source_id, replace_others=not args.keep_others)
            if not args.no_facts:
                assert profile is not None
                new_facts = [*index.facts, *derived]
                new_ids = {fact.id for fact in new_facts}
                stale = [fact.id for fact in profile.facts
                         if _replaced_provenance(fact) and fact.id not in new_ids]
                if stale:
                    profile = store.remove_facts(args.candidate, stale)
                changed = [fact for fact in new_facts
                           if not _unchanged(profile.find_fact(fact.id), fact)]
                known = {fact.id for fact in profile.facts}
                if changed:
                    profile = store.upsert_facts(args.candidate, changed)
                receipt["profile"] = {
                    "facts_removed": len(stale),
                    "facts_added": sum(1 for fact in changed if fact.id not in known),
                    "facts_updated": sum(1 for fact in changed if fact.id in known),
                    "facts_unchanged": len(new_facts) - len(changed),
                    "story_facts": len(index.facts), "derived_facts": len(derived),
                    "verified_facts_total": len(profile.verified_facts()),
                    "facts_total": len(profile.facts),
                }
                receipt["facts_index"] = knowledge.index_candidate(profile)
        receipt["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        if args.receipt is not None:
            args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json_private(args.receipt, receipt)
        if args.facts_review is not None and review_markdown is not None:
            review = facts_review(index, candidate_id=args.candidate)
            review["derived_years_facts"] = [fact.model_dump(mode="json") for fact in derived]
            args.facts_review.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json_private(args.facts_review, review)
            _write_private_text(review_markdown, facts_review_markdown(review, derived))
        print(json.dumps(receipt, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        # Parser, provider and database errors may carry document text or credentials.
        print(f"error: story indexing failed ({type(exc).__name__}); nothing was submitted",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
