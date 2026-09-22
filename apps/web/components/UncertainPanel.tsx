"use client";

import { useState, type FormEvent } from "react";
import type { ApplicationView } from "@/lib/service/types";
import { formatClock, formatDateTime } from "@/lib/format";
import type { DeskActions } from "./ApplicationDesk";
import { EvidenceList } from "./Evidence";

type Route = "found" | "not_received" | null;

export function UncertainPanel({ view, actions }: { view: ApplicationView; actions: DeskActions }) {
  const uncertain = view.uncertain;
  const [route, setRoute] = useState<Route>(null);
  const [pending, setPending] = useState<"recheck" | "found" | "not_received" | null>(null);
  const [foundIn, setFoundIn] = useState<"email" | "portal" | "other" | "">("");
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [checked, setChecked] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  if (!uncertain) return null;

  async function recheck() {
    setPending("recheck");
    await actions.reconcile({ kind: "recheck" });
    setPending(null);
  }

  async function recordFound(event: FormEvent) {
    event.preventDefault();
    if (!foundIn) {
      setErrors({ foundIn: "Say where you saw the confirmation." });
      return;
    }
    setErrors({});
    setPending("found");
    await actions.reconcile({
      kind: "user_found_confirmation",
      foundIn,
      reference: reference.trim() || null,
      note: note.trim() || null,
    });
    setPending(null);
  }

  async function recordNotReceived(event: FormEvent) {
    event.preventDefault();
    if (!checked) {
      setErrors({
        notReceived: "Confirm you've checked. Unlocking a second attempt without checking could send a duplicate.",
      });
      return;
    }
    setErrors({});
    setPending("not_received");
    await actions.reconcile({ kind: "user_confirmed_not_received" });
    setPending(null);
  }

  return (
    <div className="panel panel--uncertain">
      <p className="lede">{uncertain.reason}</p>
      <p>
        Submit was pressed at <time dateTime={uncertain.attemptedAt}>{formatDateTime(uncertain.attemptedAt)}</time>. It
        may have gone through. To avoid applying twice, this application is locked until the outcome is settled.
      </p>

      {uncertain.lastCheckedAt && (
        <div className="check-result" role="status">
          <p className="check-result__title">Last check · {formatClock(uncertain.lastCheckedAt)}</p>
          <p>{uncertain.lastCheckResult}</p>
        </div>
      )}

      <EvidenceList items={uncertain.evidence} title="What the site showed" />

      <div className="reconcile">
        <h2 className="reconcile__title">Settle the outcome</h2>
        <div className="reconcile__option">
          <button type="button" className="button button--primary" disabled={pending !== null} onClick={recheck}>
            {pending === "recheck" ? "Checking the site…" : "Check the site again"}
          </button>
          <p className="field__hint">
            Looks for a confirmation page, message or applicant-portal record. Doesn&rsquo;t resubmit.
          </p>
        </div>

        <div className="reconcile__option">
          <button
            type="button"
            className="button button--secondary"
            aria-expanded={route === "found"}
            aria-controls="reconcile-found"
            onClick={() => setRoute(route === "found" ? null : "found")}
          >
            I found a confirmation
          </button>
          {route === "found" && (
            <form id="reconcile-found" className="reconcile__form" method="post" noValidate onSubmit={recordFound}>
              <fieldset className="field choice-group" aria-describedby={errors.foundIn ? "foundIn-error" : undefined}>
                <legend className="field__label">
                  Where did you see it? <span className="tag tag--required">Required</span>
                </legend>
                <div className="choices choices--inline">
                  {(
                    [
                      ["email", "Confirmation email"],
                      ["portal", "Applicant portal"],
                      ["other", "Somewhere else"],
                    ] as const
                  ).map(([value, label]) => (
                    <label key={value} className="choice">
                      <input
                        type="radio"
                        name="foundIn"
                        value={value}
                        checked={foundIn === value}
                        aria-invalid={errors.foundIn ? true : undefined}
                        onChange={() => setFoundIn(value)}
                      />
                      <span>{label}</span>
                    </label>
                  ))}
                </div>
                {errors.foundIn && (
                  <p id="foundIn-error" className="field__error">
                    {errors.foundIn}
                  </p>
                )}
              </fieldset>
              <div className="field">
                <label htmlFor="reconcile-reference" className="field__label">
                  Confirmation reference <span className="tag">Optional</span>
                </label>
                <input
                  id="reconcile-reference"
                  className="input mono"
                  value={reference}
                  onChange={(event) => setReference(event.target.value)}
                  autoComplete="off"
                />
              </div>
              <div className="field">
                <label htmlFor="reconcile-note" className="field__label">
                  Note for the receipt <span className="tag">Optional</span>
                </label>
                <textarea
                  id="reconcile-note"
                  className="input"
                  rows={2}
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                />
              </div>
              <button type="submit" className="button button--primary" disabled={pending !== null}>
                {pending === "found" ? "Recording…" : "Record confirmation"}
              </button>
            </form>
          )}
        </div>

        <div className="reconcile__option">
          <button
            type="button"
            className="button button--secondary"
            aria-expanded={route === "not_received"}
            aria-controls="reconcile-not-received"
            onClick={() => setRoute(route === "not_received" ? null : "not_received")}
          >
            The employer has no record of it
          </button>
          {route === "not_received" && (
            <form
              id="reconcile-not-received"
              className="reconcile__form"
              method="post"
              noValidate
              onSubmit={recordNotReceived}
            >
              <div className={`attestation${errors.notReceived ? " is-invalid" : ""}`}>
                <input
                  id="not-received-confirm"
                  type="checkbox"
                  checked={checked}
                  aria-invalid={errors.notReceived ? true : undefined}
                  aria-describedby={errors.notReceived ? "not-received-confirm-error" : undefined}
                  onChange={(event) => setChecked(event.target.checked)}
                />
                <label htmlFor="not-received-confirm">
                  I checked with the employer or their applicant portal, and this application was not received.
                </label>
                {errors.notReceived && (
                  <p id="not-received-confirm-error" className="field__error">
                    {errors.notReceived}
                  </p>
                )}
              </div>
              <button type="submit" className="button button--primary" disabled={pending !== null}>
                {pending === "not_received" ? "Recording…" : "Record as not received"}
              </button>
            </form>
          )}
        </div>

        <p className="reconcile__why">
          There&rsquo;s no retry button here on purpose. Trying again before this is settled could send a second
          application.
        </p>
      </div>

      <button type="button" className="text-button" onClick={actions.startAnother}>
        Leave this locked and start a different application
      </button>
    </div>
  );
}
