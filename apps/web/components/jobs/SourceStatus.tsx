import { SOURCE_LABELS, type SearchRunView, type SourceState } from "@/lib/jobs/types";
import { formatDateTime } from "@/lib/format";

const STATE_LABELS: Record<SourceState, string> = {
  QUEUED: "Waiting",
  RUNNING: "Searching…",
  OK: "Done",
  PARTIAL: "Partly done",
  NEEDS_USER: "Needs you",
  BLOCKED: "Blocked",
  ERROR: "Failed",
  SKIPPED: "Not searched",
};

export function SourceStatus({ run }: { run: SearchRunView }) {
  const needsAttention = run.results.filter((result) => ["PARTIAL", "NEEDS_USER", "BLOCKED", "ERROR"].includes(result.state)).length;
  const count = run.results.reduce((sum, result) => sum + result.resultCount, 0);
  const results = <ul className="sources__list">
        {run.results.map((result) => (
          <li key={result.source} className={`source state-${result.state.toLowerCase()}`}>
            <p className="source__head">
              <span className="source__name">{SOURCE_LABELS[result.source] ?? result.source}</span>
              <span className="source__state">{STATE_LABELS[result.state]}</span>
            </p>
            {(result.state === "OK" || result.state === "PARTIAL") && (
              <p className="source__count">
                {result.resultCount} {result.resultCount === 1 ? "listing" : "listings"}
              </p>
            )}
            {result.message && <p className="source__message">{result.message}</p>}
            {result.userAction && (
              <p className="source__action">
                <strong>To do:</strong> {result.userAction}
              </p>
            )}
            {result.state === "BLOCKED" && !result.message && (
              <p className="source__message">The source refused access. This isn&rsquo;t an empty result.</p>
            )}
          </li>
        ))}
      </ul>;
  if (!run.finishedAt) return <section className="sources" aria-labelledby="sources-title">
    <h2 id="sources-title" className="sources__title">Searching sources</h2>
    {results}
  </section>;
  return <details className="sources sources--finished">
    <summary className="sources__summary">
      <span>Last search · {formatDateTime(run.finishedAt)}</span>
      <span className={needsAttention ? "sources__attention" : "sources__complete"}>
        {count} source {count === 1 ? "result" : "results"}{needsAttention ? ` · ${needsAttention} ${needsAttention === 1 ? "source needs" : "sources need"} attention` : " · search finished"}
      </span>
      <span className="sources__disclosure">Source details</span>
    </summary>
    {results}
  </details>;
}
