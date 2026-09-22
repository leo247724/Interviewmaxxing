"""Package-local command line: preview/apply a pipeline import and show the board.

    interviewmaxxing-pipeline preview FILE                 # validate; writes nothing
    interviewmaxxing-pipeline import FILE --expect-sha256 HEX
    interviewmaxxing-pipeline board
    interviewmaxxing-pipeline lanes

``--db`` defaults to ``$IMX_HOME/state/pipeline.sqlite3`` and ``--candidate`` to
``IMX_CANDIDATE_ID``. Preview and import output lists row numbers, import keys,
actions, lanes and field *names* only, never cell values, so it is safe to share.
``import`` requires the digest printed by ``preview``, so exactly the previewed file
is applied. Exit status: 0 success, 1 issues/rejected, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from interviewmaxxing_core import LocalPaths

from .importer import ImportFileError, load_import
from .models import ImportPreview, ImportReceipt, RowIssue
from .store import ImportRejected, PipelineStore, default_pipeline_db


def _issues(issues: Sequence[RowIssue]) -> list[dict[str, Any]]:
    return [i.model_dump(mode="json") for i in issues]


def _summary(result: ImportPreview | ImportReceipt) -> dict[str, Any]:
    source = result.source
    return {
        "candidateId": result.candidate_id,
        "source": {"name": source.name, "format": source.format,
                   "documentSha256": source.document_sha256,
                   "sourceSha256": source.source_sha256, "sheet": source.sheet,
                   "table": source.table},
        "counts": result.counts(),
        "rows": [{"sourceRow": r.source_row, "importKey": r.import_key, "action": r.action,
                  "itemId": r.item_id, "lane": r.lane, "laneRule": r.lane_rule,
                  "updatedFields": r.updated_fields, "keptManualFields": r.kept_manual_fields}
                 for r in result.rows],
    }


def main(argv: Sequence[str] | None = None) -> int:
    paths = LocalPaths.from_env()
    parser = argparse.ArgumentParser(prog="interviewmaxxing-pipeline")
    parser.add_argument("--db", type=Path, default=None,
                        help="pipeline database (default: $IMX_HOME/state/pipeline.sqlite3)")
    parser.add_argument("--candidate", default=paths.candidate_id)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="validate an import; writes nothing")
    preview.add_argument("file", type=Path)
    preview.add_argument("--format", choices=["json", "csv"])
    apply = commands.add_parser("import", help="apply a previewed import")
    apply.add_argument("file", type=Path)
    apply.add_argument("--format", choices=["json", "csv"])
    apply.add_argument("--expect-sha256", required=True,
                       help="documentSha256 printed by preview")
    commands.add_parser("board", help="print the board as JSON")
    commands.add_parser("lanes", help="print the lanes as JSON")
    args = parser.parse_args(argv)

    db = args.db or default_pipeline_db(paths)
    out: dict[str, Any]
    status = 0
    try:
        with PipelineStore.open(db) as store:
            if args.command in ("preview", "import"):
                document = load_import(args.file, format=args.format)
                if args.command == "preview":
                    result = store.preview_import(args.candidate, document)
                    out = {**_summary(result), "ok": result.ok,
                           "skippedBlankRows": result.skipped_blank_rows,
                           "issues": _issues(result.issues)}
                    status = 0 if result.ok else 1
                else:
                    try:
                        receipt = store.apply_import(
                            args.candidate, document,
                            expected_document_sha256=args.expect_sha256)
                    except ImportRejected as exc:
                        out = {"ok": False, "issues": _issues(exc.issues)}
                        status = 1
                    else:
                        out = {**_summary(receipt), "ok": True, "receiptId": receipt.id,
                               "importedAt": receipt.imported_at.isoformat()}
            elif args.command == "board":
                out = store.board(args.candidate).model_dump(mode="json")
            else:
                out = store.lanes(args.candidate).model_dump(mode="json")
    except (ImportFileError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
