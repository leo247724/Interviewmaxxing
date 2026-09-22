import { SOURCE_LABELS, type SearchRunView, type SourceState } from "@/lib/jobs/types";
import { formatClock } from "@/lib/format";

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
  return (
    <section className="sources" aria-labelledby="sources-title">
      <h2 id="sources-title" className="sources__title">
        {run.finishedAt ? `Last search · finished ${formatClock(run.finishedAt)}` : "Searching sources"}
      </h2>
      <ul className="sources__list">
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
      </ul>
    </section>
  );
}
