"use client";

import { useId, useState } from "react";
import { PREVIEW_SCENARIOS, isPreviewScenario, type PreviewScenarioId } from "@/lib/service/preview";

export function PreviewBar({
  initial,
  onChange,
  applicationOpen,
}: {
  initial: PreviewScenarioId;
  onChange: (scenario: PreviewScenarioId) => void;
  applicationOpen: boolean;
}) {
  const [scenario, setScenario] = useState<PreviewScenarioId>(initial);
  const selectId = useId();
  const summary = PREVIEW_SCENARIOS.find((item) => item.id === scenario)?.summary;

  return (
    <section className="preview-bar" aria-label="Preview controls">
      <div className="preview-bar__inner">
        <p className="preview-bar__tag">Preview</p>
        <p className="preview-bar__note">
          Fictional candidate, fictional job sites. Nothing leaves this page and nothing is submitted.
        </p>
        <div className="preview-bar__control">
          <label htmlFor={selectId}>Scenario</label>
          <select
            id={selectId}
            value={scenario}
            onChange={(event) => {
              const next = event.target.value;
              if (isPreviewScenario(next)) {
                setScenario(next);
                onChange(next);
              }
            }}
          >
            {PREVIEW_SCENARIOS.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </div>
        <p className="preview-bar__summary">
          {summary}
          {applicationOpen && " Applies to the next application you start."}
        </p>
      </div>
    </section>
  );
}
