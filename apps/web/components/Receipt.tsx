import type { ApplicationView, SubmissionReceiptView } from "@/lib/service/types";
import { formatDateTime, timeZoneName } from "@/lib/format";
import { receiptAuthority } from "@/lib/receipt";
import { EvidenceList } from "./Evidence";

export function Receipt({
  view,
  receipt,
  mode,
  onStartAnother,
}: {
  view: ApplicationView;
  receipt: SubmissionReceiptView;
  mode: "live" | "preview";
  onStartAnother: () => void;
}) {
  const authority = receiptAuthority(receipt);

  return (
    <article className={`receipt${mode === "preview" ? " receipt--preview" : ""}`} aria-labelledby="receipt-title">
      <header className="receipt__head">
        <h2 id="receipt-title" className="receipt__title">
          Submission receipt
        </h2>
        <p className="receipt__id mono">{receipt.receiptId}</p>
        {mode === "preview" && <p className="receipt__preview">Preview receipt — fictional, nothing was sent.</p>}
      </header>

      <dl className="receipt__rows">
        <div>
          <dt>Job</dt>
          <dd>
            {view.job.title ?? "Title not identified"}
            {view.job.company && <span className="receipt__sub"> · {view.job.company}</span>}
            {view.job.ats && <span className="receipt__sub"> · via {view.job.ats}</span>}
          </dd>
        </div>
        <div>
          <dt>Application page</dt>
          <dd className="mono receipt__url">
            <a href={view.applicationUrl} target="_blank" rel="noreferrer noopener">
              {view.applicationUrl}
            </a>
          </dd>
        </div>
        <div>
          <dt>Submitted</dt>
          <dd>
            <time dateTime={receipt.submittedAt}>{formatDateTime(receipt.submittedAt)}</time>
            <span className="receipt__sub mono"> {timeZoneName()}</span>
          </dd>
        </div>
        <div>
          <dt>Confirmation reference</dt>
          <dd>
            {receipt.confirmationReference ? (
              <span className="mono receipt__reference">{receipt.confirmationReference}</span>
            ) : (
              <span className="receipt__none">
                {authority.byUser ? "None recorded." : "None shown. The evidence below is the confirmation."}
              </span>
            )}
          </dd>
        </div>
        <div>
          <dt>How it was confirmed</dt>
          <dd data-testid="confirmation-method">{authority.description}</dd>
        </div>
        {view.resumeFileName && (
          <div>
            <dt>Resume sent</dt>
            <dd className="mono">{view.resumeFileName}</dd>
          </div>
        )}
      </dl>

      {authority.byUser && (
        <p className="receipt__caveat">
          Marked submitted on your report, not on the site&rsquo;s confirmation. Any site screenshots below are from
          before your report and don&rsquo;t show acceptance.
        </p>
      )}

      <EvidenceList items={receipt.evidence} />

      <footer className="receipt__foot">
        <button type="button" className="button button--secondary" onClick={onStartAnother}>
          Start another application
        </button>
      </footer>
    </article>
  );
}
