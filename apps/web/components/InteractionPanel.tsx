"use client";

import { useState } from "react";
import type { InputRequestView } from "@/lib/service/types";
import type { DeskActions } from "./ApplicationDesk";

type InteractionNeeds = Extract<InputRequestView, { kind: "interaction" }>;

const CONTINUE_LABEL: Record<InteractionNeeds["interaction"], string> = {
  SIGN_IN: "I've signed in — continue",
  CAPTCHA: "I've completed it — continue",
  VERIFICATION: "I've verified — continue",
};

export function InteractionPanel({ needs, actions }: { needs: InteractionNeeds; actions: DeskActions }) {
  const [pending, setPending] = useState(false);

  return (
    <div className="panel panel--interaction">
      <p className="lede">{needs.instructions}</p>
      {needs.pageUrl && (
        <p className="panel__where">
          <span className="panel__where-label">Page in the browser window</span>
          <span className="mono">{needs.pageUrl}</span>
        </p>
      )}
      <p className="field__hint">
        The desk can&rsquo;t do this step for you. Your sign-in details are entered only on the site itself.
      </p>
      <div className="panel__actions">
        <button
          type="button"
          className="button button--primary"
          disabled={pending}
          onClick={async () => {
            setPending(true);
            await actions.resume();
            setPending(false);
          }}
        >
          {pending ? "Checking the page…" : CONTINUE_LABEL[needs.interaction]}
          <span aria-hidden="true" className="button__arrow">
            →
          </span>
        </button>
      </div>
    </div>
  );
}
