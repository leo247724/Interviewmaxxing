"use client";

import { useState } from "react";
import type { ImportPreviewView, ImportReceiptView, PipelineService } from "@/lib/pipeline/types";
import { asServiceError } from "@/lib/service/errors";
import { formatDateTime } from "@/lib/format";

const MAX_IMPORT_BYTES = 2 * 1024 * 1024;

export function ImportPanel({ service, onImported }: { service: PipelineService; onImported: () => void }) {
  const [preview, setPreview] = useState<ImportPreviewView | null>(null);
  const [receipt, setReceipt] = useState<ImportReceiptView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sourceId, setSourceId] = useState("my-pipeline");

  async function choose(file: File) {
    setError(null);
    setPreview(null);
    setReceipt(null);
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(sourceId.trim()))
      return setError("Give this tracker a name using letters, digits, dots, dashes or underscores.");
    const lower = file.name.toLowerCase();
    const format = lower.endsWith(".csv") ? "csv" : lower.endsWith(".json") ? "json" : null;
    if (!format) return setError("Choose a CSV or JSON file.");
    if (file.size > MAX_IMPORT_BYTES) return setError("Import files must be 2 MB or smaller.");
    setBusy(true);
    try {
      setPreview(await service.previewImport({ format, fileName: file.name, content: await file.text(), sourceId: sourceId.trim() }));
    } catch (caught) {
      setError(asServiceError(caught).message);
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      setReceipt(await service.commitImport(preview.previewId));
      setPreview(null);
      onImported();
    } catch (caught) {
      setError(asServiceError(caught).message);
    } finally {
      setBusy(false);
    }
  }

  const importable = preview ? preview.counts.create + preview.counts.update : 0;

  return (
    <div className="import">
      <p className="lede">
        Bring rows in from a CSV or JSON export that uses the tracker&rsquo;s column names. You&rsquo;ll see every row
        before anything is saved. Importing the same file again doesn&rsquo;t duplicate cards or undo your edits.
      </p>
      <div className="field">
        <label htmlFor="import-source" className="field__label">Tracker name</label>
        <input id="import-source" className="input" value={sourceId} disabled={busy} aria-describedby="import-source-hint" onChange={(event) => {
          setSourceId(event.target.value);
          setPreview(null);
          setReceipt(null);
        }} />
        <p id="import-source-hint" className="field__hint">Reuse this name for updated exports of the same tracker, even if the file name changes. Use a different name for a separate tracker.</p>
      </div>
      <div className="upload">
        <input
          id="import-file"
          className="upload__input"
          type="file"
          accept=".csv,.json,text/csv,application/json"
          disabled={busy}
          aria-describedby={`import-file-hint${error ? " import-file-error" : ""}`}
          onChange={async (event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) await choose(file);
          }}
        />
        <label htmlFor="import-file" className="button button--secondary upload__button">
          {busy && !preview ? "Reading…" : "Choose a file"}
        </label>
        <p id="import-file-hint" className="field__hint">
          CSV or JSON, up to 2 MB. Headers such as Company, Role, Stage and Fit / 10.
        </p>
        {error && (
          <p id="import-file-error" className="field__error" role="alert">
            {error}
          </p>
        )}
      </div>

      {preview && (
        <section className="import__preview" aria-labelledby="import-preview-title">
          <h3 id="import-preview-title" className="import__title">
            {preview.fileName}: {preview.rows.length} {preview.rows.length === 1 ? "row" : "rows"}
          </h3>
          <p className="import__counts" role="status">
            {preview.counts.create} new · {preview.counts.update} updated · {preview.counts.unchanged} unchanged
            {preview.counts.error > 0 && <strong> · {preview.counts.error} with problems</strong>}
          </p>
          <div className="table-scroll">
            <table className="import__table">
              <thead>
                <tr>
                  <th scope="col">Row</th>
                  <th scope="col">Company</th>
                  <th scope="col">Role</th>
                  <th scope="col">Result</th>
                </tr>
              </thead>
              <tbody>
                {preview.rows.map((row) => (
                  <tr key={row.rowNumber} className={row.action === "error" ? "is-error" : undefined}>
                    <td className="mono">{row.rowNumber}</td>
                    <td>{row.company ?? "—"}</td>
                    <td>{row.role ?? "—"}</td>
                    <td>
                      {row.action === "error" ? (
                        <ul className="import__errors">
                          {row.errors.map((item, index) => (
                            <li key={index}>
                              <strong>{item.field}:</strong> {item.message}
                            </li>
                          ))}
                        </ul>
                      ) : row.action === "create" ? (
                        "New card"
                      ) : row.action === "update" ? (
                        "Updates fields you haven’t edited"
                      ) : (
                        "Unchanged"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {preview.counts.error > 0 ? (
            <p className="form-alert">
              Nothing will be imported while any row has a problem. Fix those rows in the file and choose it again.
            </p>
          ) : (
            <button type="button" className="button button--primary" disabled={busy} onClick={commit}>
              {busy
                ? "Importing…"
                : importable === 0
                  ? "Record this import"
                  : `Import ${importable} ${importable === 1 ? "row" : "rows"}`}
            </button>
          )}
        </section>
      )}

      {receipt && (
        <div className="import__receipt" role="status">
          <p className="import__title">Imported {receipt.fileName}</p>
          <p>
            {receipt.created} new, {receipt.updated} updated, {receipt.unchanged} unchanged ·{" "}
            {formatDateTime(receipt.importedAt)} · digest{" "}
            <span className="mono">{receipt.sourceDigest.slice(0, 12)}…</span>
          </p>
        </div>
      )}
    </div>
  );
}
