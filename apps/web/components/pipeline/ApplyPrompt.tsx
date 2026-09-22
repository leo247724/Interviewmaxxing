"use client";

import { useState, type FormEvent } from "react";
import { validateApplicationUrl } from "@/lib/validation";
import { FieldMessages, describedBy } from "../fields";

/** Confirms or asks for the application link before handing a job to the desk. */
export function ApplyPrompt({
  initialUrl,
  savesTo,
  onSubmit,
}: {
  initialUrl: string | null;
  /** Where a newly entered link is saved, e.g. "the card". Null when it isn't saved. */
  savesTo: string | null;
  onSubmit: (applicationUrl: string) => Promise<string | null>;
}) {
  const [url, setUrl] = useState(initialUrl ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const problem = validateApplicationUrl(url);
    setError(problem);
    if (problem) return;
    setBusy(true);
    const failure = await onSubmit(url.trim());
    setBusy(false);
    if (failure) setError(failure);
  }

  return (
    <form className="apply-prompt" method="post" noValidate onSubmit={submit}>
      <p className="lede">
        {initialUrl
          ? "The desk will open with this link filled in. Nothing is sent until you press Apply and submit there."
          : `There's no application link yet. Paste the page where the application form starts.${savesTo ? ` It's saved to ${savesTo}, then` : " Then"} the desk opens with it filled in.`}
      </p>
      <div className={`field${error ? " is-invalid" : ""}`}>
        <label htmlFor="apply-url" className="field__label">
          Application link <span className="tag tag--required">Required</span>
        </label>
        <input
          id="apply-url"
          className="input mono"
          type="url"
          inputMode="url"
          spellCheck={false}
          value={url}
          autoFocus
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy("apply-url", "hint", error)}
          onChange={(event) => setUrl(event.target.value)}
        />
        <FieldMessages
          id="apply-url"
          hint="A company homepage or job-board search page isn't an application link."
          error={error}
        />
      </div>
      <button type="submit" className="button button--primary" disabled={busy}>
        {busy ? "Saving…" : "Continue to the desk"}
        <span aria-hidden="true" className="button__arrow">
          →
        </span>
      </button>
    </form>
  );
}
