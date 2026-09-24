#!/usr/bin/env python3
"""Export, validate or import the editable contact answer map. No browser actions.

Run from the repository: uv run --no-sync python scripts/simple_answers.py --help
Import means the user has checked these values for reuse as their contact details.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_candidate.files import read_json, write_json_private
from interviewmaxxing_candidate.simple_answers import SimpleAnswers
from interviewmaxxing_core import (
    CandidateNotFound,
    CandidateProfileInvalid,
    LocalPaths,
    utc_now,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "validate", "import"))
    parser.add_argument("--file", type=Path, required=True, help="Private editable JSON map")
    parser.add_argument("--home", type=Path, help="IMX data home; defaults to IMX_HOME")
    parser.add_argument("--candidate-id", default=os.environ.get("IMX_CANDIDATE_ID", "default"))
    args = parser.parse_args()
    path = args.file.expanduser().resolve()
    try:
        store = LocalCandidateStore.from_paths(LocalPaths.from_env(home=args.home))
        if args.command == "export":
            if path.exists():
                raise ValueError("Output already exists; choose a new file to keep your edits.")
            answers = SimpleAnswers.from_profile(store.load(args.candidate_id))
            write_json_private(path, answers.model_dump())
        else:
            answers = SimpleAnswers.model_validate(read_json(path))
        values = answers.model_dump()
        result: dict[str, object] = {
            "command": args.command,
            "file": str(path),
            "filled_keys": [key for key, value in values.items() if value is not None],
            "unanswered_keys": [key for key, value in values.items() if value is None],
            "profile_updated": False,
            "saved_answers_updated": 0,
        }
        if args.command in {"validate", "import"}:
            # Validate conversion before accessing or writing a live profile.
            identity = answers.to_identity(confirmed_at=utc_now())
            answers.saved_answer_updates(confirmed_at=identity.verified_at)
            if args.command == "import":
                current = store.load(args.candidate_id)
                updates = answers.saved_answer_updates(
                    confirmed_at=identity.verified_at, current=current.saved_answers
                )
                identity = answers.to_identity(
                    confirmed_at=identity.verified_at, current=current.identity
                )
                changed = identity != current.identity
                if changed:
                    store.upsert_profile(
                        args.candidate_id, identity=identity, resume_id=current.resume.id
                    )
                result["profile_updated"] = changed
                result["profile_path"] = str(store.profile_path(args.candidate_id))
                for answer in updates:
                    store.save_answer(args.candidate_id, answer)
                result["saved_answers_updated"] = len(updates)
        print(json.dumps(result, indent=2))
        return 0
    except ValidationError as exc:
        # Field locations and messages suffice; never echo contact values to logs.
        problems = [
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
        ]
        print("Invalid answer map: " + "; ".join(problems), file=sys.stderr)
    except (CandidateNotFound, CandidateProfileInvalid, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
