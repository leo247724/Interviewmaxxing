"use client";

import { useState } from "react";
import type { ApplicationView } from "@/lib/service/types";
import { preparationOf, reviewOf } from "@/lib/preparation";
import type { DeskActions } from "./ApplicationDesk";
import { EvidenceList } from "./Evidence";
import { QuestionsForm } from "./QuestionsForm";
import { ReviewList } from "./ReviewList";

/**
 * A prepared application: every page was filled and the desk stopped at the
 * site's final review step. Nothing was submitted. The person checks what was
 * entered against the screenshot; a remaining question is asked here too.
 */
export function PreparedPanel({ view, actions }: { view: ApplicationView; actions: DeskActions }) {
  const [pending, setPending] = useState(false);
  const preparation = preparationOf(view);
  if (!preparation) return null;
  const questions = view.needs?.kind === "questions" ? view.needs : null;

  return (
    <div className="panel prepared">
      <p className="lede">
        Every page of the form is filled in, and the desk stopped at the site&rsquo;s final review step.{" "}
        <strong>Nothing was submitted</strong>: the site hasn&rsquo;t received this application.
      </p>

      {preparation.captchaPending && (
        <div className="notice notice--captcha" data-testid="captcha-note">
          <p className="notice__title">A CAPTCHA is waiting on the form</p>
          <p>
            It must be solved in the browser before this application can be submitted. The desk doesn&rsquo;t solve
            CAPTCHAs.
          </p>
        </div>
      )}

      {questions && <QuestionsForm key={view.id} needs={questions} actions={actions} />}

      <div className="prepared__evidence">
        {preparation.evidence.length > 0 ? (
          <EvidenceList items={preparation.evidence} title="The filled review page" />
        ) : (
          <p className="field__hint">The service saved no screenshot of the review page.</p>
        )}
      </div>

      <ReviewList items={reviewOf(view)} />

      {preparation.formUrl && (
        <p className="panel__where">
          <span className="panel__where-label">Final review page</span>
          <span className="mono">{preparation.formUrl}</span>
        </p>
      )}

      <p className="field__hint">
        <strong>Prepare again</strong> re-reads the site and fills the form again, then stops at the same review step.
        Submitting stays turned off.
      </p>
      <div className="panel__actions">
        <button
          type="button"
          className="button button--secondary"
          disabled={pending}
          onClick={async () => {
            setPending(true);
            await actions.resume();
            setPending(false);
          }}
        >
          {pending ? "Preparing again…" : "Prepare again"}
        </button>
        <button type="button" className="button button--secondary" onClick={actions.startAnother}>
          Start a different application
        </button>
      </div>
    </div>
  );
}
