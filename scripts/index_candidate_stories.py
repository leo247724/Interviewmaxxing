#!/usr/bin/env python3
"""Index the candidate's own professional stories (a .docx) into the private RAG store
and add the atomic facts they state, and the years of experience the resume timeline
implies, to the candidate profile.

The document is parsed with the standard library, split into stories at its headings (a
Markdown file at its H1 and H2 headings) and analysed. Each story is then linked to a resume
role: by the company name the story states distinctively (every distinctive word of it, or
the full name; a common word such as "Growth" never links), or else by one Jev decision,
gated at confidence 0.90 and probability 0.95, over the resume roles its own stated period
overlaps. A story whose stated period (its heading's "Aug 2019 - May 2020", "Jun 2025 -
Present") shares no month with the proposed role is not linked: it keeps its stated period
and the review names the mismatch. A linked story's chunks and facts carry the resume role's
dates; an unlinked story's carry the period or year it states; never a default year. The
chunks (one summary plus sections of about 120-300 words, each headed by the story's
title, employer, resume role, period and themes) are indexed under the ``story`` kind as
the candidate's one current stories source; unchanged documents need no new embeddings.
Concrete first-person singular sentences become ``CandidateFact`` records with
``story:<chunk id>`` provenance, UNVERIFIED: the person confirms the ones they stand behind
through the facts import (``--facts-review`` writes a ``.confirm.json`` in that format; see
docs/rag-writing.md). Years of experience are derived from the dated resume roles with
provenance ``derived:experience_timeline``: the total is verified (it restates the confirmed
role dates), each per-area fact is UNVERIFIED until confirmed the same way. Facts of either
provenance that this run does not produce again are removed from the profile, the new ones
are merged through the candidate store (a fact the person confirmed through the import is
kept by id and never downgraded), and the verified fact projection is re-indexed.

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
from interviewmaxxing_browser.ai.stories import link_stories
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_candidate.files import write_json_private
from interviewmaxxing_core import CandidateFact, CandidateProfile, LocalPaths, utc_now
from interviewmaxxing_generation.knowledge.stories import (
    DEFAULT_STORY_SOURCE,
    build_story_index,
    confirmable_facts,
    facts_review,
    facts_review_markdown,
    read_stories,
    resume_roles,
    story_index_receipt,
    story_source_of,
    superseded_story_facts,
)
from interviewmaxxing_generation.knowledge.timeline import DERIVED_SOURCE, derive_experience_years
from interviewmaxxing_selection.credentials import load_api_key
from interviewmaxxing_selection.jev import JevClient


def _unchanged(existing: CandidateFact | None, fact: CandidateFact) -> bool:
    """The same fact, with the same verification status, is already in the profile: a
    verified one keeps its verification time, an unverified one stays as it is."""
    return (existing is not None
            and existing.verification.status is fact.verification.status
            and (existing.key, existing.value, existing.source, list(existing.evidence))
            == (fact.key, fact.value, fact.source, list(fact.evidence)))


def _confirmed(existing: CandidateFact | None) -> bool:
    """The person confirmed this fact through the facts import (``user:`` provenance,
    verified): the run never replaces or downgrades it."""
    return existing is not None and existing.is_verified and existing.source.startswith("user:")


def _merge(store: LocalCandidateStore, candidate: str, profile: CandidateProfile,
           new_facts: list[CandidateFact], stale: list[str]) -> tuple[CandidateProfile, dict[str, int]]:
    """Remove the stale facts, merge the changed new ones; unchanged facts keep their
    verification, confirmed ones are kept by id. Returns the profile and the counts."""
    if stale:
        profile = store.remove_facts(candidate, stale)
    confirmed = [fact for fact in new_facts if _confirmed(profile.find_fact(fact.id))]
    changed = [fact for fact in new_facts if fact not in confirmed
               and not _unchanged(profile.find_fact(fact.id), fact)]
    known = {fact.id for fact in profile.facts}
    if changed:
        profile = store.upsert_facts(candidate, changed)
    return profile, {"removed": len(stale), "added": sum(1 for fact in changed if fact.id not in known),
                     "updated": sum(1 for fact in changed if fact.id in known),
                     "unchanged": len(new_facts) - len(changed) - len(confirmed),
                     "kept_confirmed": len(confirmed)}


def _derived_counts(derived: list[CandidateFact]) -> dict[str, int]:
    """Counts only: the values are the person's to review in the private files."""
    return {"count": len(derived), "verified": sum(1 for fact in derived if fact.is_verified),
            "unverified": sum(1 for fact in derived if not fact.is_verified)}


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, required=True, help="the stories .docx, .md or .txt")
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
    confirm_file = args.facts_review.with_suffix(".confirm.json") if args.facts_review else None
    for path in (args.receipt, args.facts_review, review_markdown, confirm_file):
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
        today = datetime.now(UTC).date()
        # Names first, then the gated decision over the roles a story's stated period
        # overlaps; the index drops a link the story's own stated period contradicts.
        links, link_traces = link_stories(document.stories, document.analyses, roles,
                                          decide=decisions.decide if decisions is not None else None,
                                          model=decisions.model if decisions is not None else "",
                                          today=today)
        index = build_story_index(document, verified_at=now, links=links, source_id=args.source_id)
        # Facts the person confirmed before their story's link changed: marked superseded in
        # the confirm file for removal (the index never removes a confirmed fact itself).
        superseded = superseded_story_facts(profile, index) if profile is not None else []
        # Years of experience come from the resume timeline alone (titles and self-dated
        # bullets), so the preview equals what the real run derives after the merge.
        derived = derive_experience_years(profile, today=today, verified_at=now) if profile is not None else []
        receipt: dict[str, Any] = {
            "candidate_sha256": hashlib.sha256(args.candidate.encode()).hexdigest(),
            "source_id": args.source_id, "dry_run": args.dry_run, "facts_enabled": not args.no_facts,
            "file_name_sha256": hashlib.sha256(args.file.name.encode()).hexdigest(),
            "resume_roles": len(roles), "link_decisions": link_traces,
            "jev_links": ("skipped: dry run" if args.dry_run else "skipped: --no-links" if args.no_links
                          else "skipped: no resume roles" if not roles else "asked"),
            **story_index_receipt(index),
            "story_facts_unverified": sum(1 for fact in index.facts if not fact.is_verified),
            "derived_years_facts": _derived_counts(derived),
            "superseded_confirmed_fact_ids": [row["id"] for row in superseded],
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
                # This source's story facts replace the earlier ones of the same source;
                # other sources' story facts stay.
                story_ids = {fact.id for fact in index.facts}
                stale_story = [fact.id for fact in profile.facts
                               if story_source_of(fact) == args.source_id and fact.id not in story_ids]
                profile, story_counts = _merge(store, args.candidate, profile, list(index.facts), stale_story)
                # Years of experience come from the whole profile (every stories source's
                # linked facts), so they are derived after the story facts are merged.
                derived = derive_experience_years(profile, today=today, verified_at=now)
                derived_ids = {fact.id for fact in derived}
                stale_derived = [fact.id for fact in profile.facts
                                 if fact.source == DERIVED_SOURCE and fact.id not in derived_ids]
                profile, derived_counts = _merge(store, args.candidate, profile, derived, stale_derived)
                receipt["derived_years_facts"] = _derived_counts(derived)
                receipt["profile"] = {
                    "story_facts": story_counts, "derived_facts": derived_counts,
                    "story_facts_of_this_source": len(index.facts),
                    "story_facts_all_sources": sum(1 for fact in profile.facts if story_source_of(fact)),
                    "confirmed_import_facts": sum(1 for fact in profile.facts
                                                  if fact.source.startswith("user:") and fact.is_verified),
                    "verified_facts_total": len(profile.verified_facts()),
                    "unverified_facts_total": sum(1 for fact in profile.facts if not fact.is_verified),
                    "facts_total": len(profile.facts),
                }
                receipt["facts_index"] = knowledge.index_candidate(profile)
        receipt["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        if args.receipt is not None:
            args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json_private(args.receipt, receipt)
        if args.facts_review is not None and review_markdown is not None:
            review = facts_review(index, candidate_id=args.candidate, roles=roles)
            review["superseded"] = superseded
            review["derived_years_facts"] = [fact.model_dump(mode="json") for fact in derived]
            args.facts_review.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json_private(args.facts_review, review)
            _write_private_text(review_markdown, facts_review_markdown(review, derived))
            # The facts offered for confirmation, in the import's format: delete the rows
            # you do not confirm, then `scripts/rag_answers.py import-facts --file <it>`.
            confirm_path = args.facts_review.with_suffix(".confirm.json")
            write_json_private(confirm_path, [*confirmable_facts([*index.facts, *derived]), *superseded])
            receipt["confirm_file"] = str(confirm_path)
        print(json.dumps(receipt, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        # Parser, provider and database errors may carry document text or credentials.
        print(f"error: story indexing failed ({type(exc).__name__}); nothing was submitted",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
