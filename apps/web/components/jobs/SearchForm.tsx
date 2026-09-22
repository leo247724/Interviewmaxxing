"use client";

import { useState, type FormEvent } from "react";
import {
  DEFAULT_PREFERENCES,
  SOURCE_LABELS,
  type CompensationPeriod,
  type SearchPreferencesView,
} from "@/lib/jobs/types";
import { validatePreferences } from "@/lib/jobs/validation";
import { FieldMessages, describedBy } from "../fields";
import { ErrorSummary } from "../ErrorSummary";

export type PreferencesInput = Omit<SearchPreferencesView, "fingerprint">;

interface Draft {
  titles: string;
  keywords: string;
  excludedKeywords: string;
  excludedCompanies: string;
  onsiteEnabled: boolean;
  onsiteLocation: string;
  onsite: boolean;
  hybrid: boolean;
  remoteEnabled: boolean;
  remoteRegion: string;
  minimum: string;
  period: CompensationPeriod;
  unknownCompensation: "KEEP" | "REVIEW";
  sources: string[];
  maxResults: string;
}

function toDraft(prefs: PreferencesInput): Draft {
  const onsite = prefs.onsite[0];
  return {
    titles: prefs.titlePhrases.join("\n"),
    keywords: prefs.keywords.join(", "),
    excludedKeywords: prefs.excludedKeywords.join(", "),
    excludedCompanies: prefs.excludedCompanies.join(", "),
    onsiteEnabled: Boolean(onsite),
    onsiteLocation: onsite?.location ?? "Austin, TX",
    onsite: onsite ? onsite.arrangements.includes("ONSITE") : true,
    hybrid: onsite ? onsite.arrangements.includes("HYBRID") : true,
    remoteEnabled: Boolean(prefs.remote),
    remoteRegion: prefs.remote?.eligibleRegion ?? "United States",
    minimum: prefs.minimumCompensation ? String(prefs.minimumCompensation.amount) : "",
    period: prefs.minimumCompensation?.period ?? "YEAR",
    unknownCompensation: prefs.unknownCompensation,
    sources: [...prefs.sources],
    maxResults: String(prefs.maxResultsPerSource),
  };
}

const list = (value: string, separator: RegExp) =>
  value
    .split(separator)
    .map((item) => item.trim())
    .filter(Boolean);

function fromDraft(draft: Draft): PreferencesInput {
  const minimum = draft.minimum.replace(/[$,\s]/g, "");
  return {
    titlePhrases: list(draft.titles, /\n/),
    keywords: list(draft.keywords, /,/),
    excludedKeywords: list(draft.excludedKeywords, /,/),
    excludedCompanies: list(draft.excludedCompanies, /,/),
    onsite: draft.onsiteEnabled
      ? [
          {
            location: draft.onsiteLocation.trim(),
            arrangements: [
              ...(draft.onsite ? (["ONSITE"] as const) : []),
              ...(draft.hybrid ? (["HYBRID"] as const) : []),
            ],
          },
        ]
      : [],
    remote: draft.remoteEnabled ? { eligibleRegion: draft.remoteRegion.trim() } : null,
    minimumCompensation: minimum ? { amount: Number(minimum), currency: "USD", period: draft.period } : null,
    unknownCompensation: draft.unknownCompensation,
    sources: draft.sources,
    maxResultsPerSource: Number(draft.maxResults),
  };
}

export function SearchForm({
  initial,
  busy,
  onSearch,
  onSave,
  serverErrors,
}: {
  initial: PreferencesInput;
  busy: "search" | "save" | null;
  onSearch: (input: PreferencesInput) => void;
  onSave: (input: PreferencesInput) => void;
  serverErrors: Record<string, string>;
}) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(initial));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [attempt, setAttempt] = useState(0);
  const shown = { ...serverErrors, ...errors };
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((current) => ({ ...current, [key]: value }));

  const order = ["titlePhrases", "onsite", "remote", "minimumCompensation", "sources", "maxResultsPerSource"];
  const targets: Record<string, string> = {
    titlePhrases: "js-titles",
    onsite: "js-onsite-location",
    remote: "js-remote-region",
    minimumCompensation: "js-minimum",
    sources: "js-sources",
    maxResultsPerSource: "js-max",
  };
  const summary = order.filter((key) => shown[key]).map((key) => ({ fieldId: targets[key], message: shown[key] }));

  function submit(kind: "search" | "save") {
    const input = fromDraft(draft);
    const found = validatePreferences(input);
    if (draft.minimum.trim() && !/^\d+(\.\d+)?$/.test(draft.minimum.replace(/[$,\s]/g, ""))) {
      found.minimumCompensation = "Enter the minimum as a number, for example 100000.";
    }
    setErrors(found);
    setAttempt((count) => count + 1);
    if (Object.keys(found).length) return;
    if (kind === "search") onSearch(input);
    else onSave(input);
  }

  return (
    <form
      className="search"
      method="post"
      noValidate
      aria-labelledby="search-title"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        submit("search");
      }}
    >
      <h2 id="search-title" className="search__title">
        Search and preferences
      </h2>
      <ErrorSummary title="Check the search settings" items={summary} attempt={attempt} />

      <div className={`field${shown.titlePhrases ? " is-invalid" : ""}`}>
        <label htmlFor="js-titles" className="field__label">
          Job titles
        </label>
        <textarea
          id="js-titles"
          className="input"
          rows={3}
          value={draft.titles}
          aria-invalid={shown.titlePhrases ? true : undefined}
          aria-describedby={describedBy("js-titles", "hint", shown.titlePhrases)}
          onChange={(event) => set("titles", event.target.value)}
        />
        <FieldMessages id="js-titles" hint="One per line. Each is searched as a phrase." error={shown.titlePhrases} />
      </div>

      <div className="field">
        <label htmlFor="js-keywords" className="field__label">
          Extra keywords <span className="tag">Optional</span>
        </label>
        <input
          id="js-keywords"
          className="input"
          value={draft.keywords}
          aria-describedby="js-keywords-hint"
          onChange={(event) => set("keywords", event.target.value)}
        />
        <FieldMessages id="js-keywords" hint="Comma-separated, e.g. lifecycle, demand generation." />
      </div>

      <fieldset
        className={`search__group${shown.onsite ? " is-invalid" : ""}`}
        aria-describedby={shown.onsite ? "js-onsite-error" : undefined}
      >
        <legend className="field__label">Onsite and hybrid</legend>
        <label className="check">
          <input
            type="checkbox"
            checked={draft.onsiteEnabled}
            onChange={(event) => set("onsiteEnabled", event.target.checked)}
          />
          <span>Include roles in a city</span>
        </label>
        {draft.onsiteEnabled && (
          <>
            <label htmlFor="js-onsite-location" className="visually-hidden">
              City for onsite and hybrid roles
            </label>
            <input
              id="js-onsite-location"
              className="input"
              value={draft.onsiteLocation}
              onChange={(event) => set("onsiteLocation", event.target.value)}
            />
            <div className="check-row">
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.onsite}
                  onChange={(event) => set("onsite", event.target.checked)}
                />
                <span>Onsite</span>
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={draft.hybrid}
                  onChange={(event) => set("hybrid", event.target.checked)}
                />
                <span>Hybrid</span>
              </label>
            </div>
          </>
        )}
        {shown.onsite && (
          <p id="js-onsite-error" className="field__error">
            {shown.onsite}
          </p>
        )}
      </fieldset>

      <fieldset className={`search__group${shown.remote ? " is-invalid" : ""}`} aria-describedby="js-remote-hint">
        <legend className="field__label">Remote</legend>
        <label className="check">
          <input
            type="checkbox"
            checked={draft.remoteEnabled}
            onChange={(event) => set("remoteEnabled", event.target.checked)}
          />
          <span>Include remote roles open to</span>
        </label>
        {draft.remoteEnabled && (
          <>
            <label htmlFor="js-remote-region" className="visually-hidden">
              Where remote roles must allow you to work
            </label>
            <input
              id="js-remote-region"
              className="input"
              value={draft.remoteRegion}
              aria-invalid={shown.remote ? true : undefined}
              onChange={(event) => set("remoteRegion", event.target.value)}
            />
          </>
        )}
        <FieldMessages
          id="js-remote"
          hint="Remote roles are searched nationwide for this region, not limited to your city or state."
          error={shown.remote}
        />
      </fieldset>

      <fieldset className={`search__group${shown.minimumCompensation ? " is-invalid" : ""}`}>
        <legend className="field__label">Minimum pay</legend>
        <div className="pay-row">
          <label htmlFor="js-minimum" className="visually-hidden">
            Minimum pay in US dollars
          </label>
          <span className="pay-row__unit" aria-hidden="true">
            USD
          </span>
          <input
            id="js-minimum"
            className="input"
            inputMode="numeric"
            value={draft.minimum}
            aria-invalid={shown.minimumCompensation ? true : undefined}
            aria-describedby={describedBy("js-minimum", "hint", shown.minimumCompensation)}
            onChange={(event) => set("minimum", event.target.value)}
          />
          <label htmlFor="js-period" className="visually-hidden">
            Pay period
          </label>
          <select
            id="js-period"
            className="input"
            value={draft.period}
            onChange={(event) => set("period", event.target.value as CompensationPeriod)}
          >
            <option value="YEAR">per year</option>
            <option value="MONTH">per month</option>
            <option value="HOUR">per hour</option>
          </select>
        </div>
        <FieldMessages id="js-minimum" hint="Leave blank for no minimum." error={shown.minimumCompensation} />
        <p className="field__label field__label--sub">When a listing doesn&rsquo;t state pay</p>
        <div className="check-row">
          <label className="check">
            <input
              type="radio"
              name="unknown-pay"
              checked={draft.unknownCompensation === "KEEP"}
              onChange={() => set("unknownCompensation", "KEEP")}
            />
            <span>Keep it</span>
          </label>
          <label className="check">
            <input
              type="radio"
              name="unknown-pay"
              checked={draft.unknownCompensation === "REVIEW"}
              onChange={() => set("unknownCompensation", "REVIEW")}
            />
            <span>Hold it for my review</span>
          </label>
        </div>
      </fieldset>

      <fieldset id="js-sources" className={`search__group${shown.sources ? " is-invalid" : ""}`} tabIndex={-1}>
        <legend className="field__label">Sources</legend>
        <div className="check-grid">
          {Object.entries(SOURCE_LABELS).map(([id, label]) => (
            <label key={id} className="check">
              <input
                type="checkbox"
                checked={draft.sources.includes(id)}
                onChange={(event) =>
                  set(
                    "sources",
                    event.target.checked ? [...draft.sources, id] : draft.sources.filter((source) => source !== id),
                  )
                }
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
        {shown.sources && <p className="field__error">{shown.sources}</p>}
      </fieldset>

      <details className="search__more">
        <summary>More options</summary>
        <div className="field">
          <label htmlFor="js-excluded" className="field__label">
            Leave out keywords <span className="tag">Optional</span>
          </label>
          <input
            id="js-excluded"
            className="input"
            value={draft.excludedKeywords}
            onChange={(event) => set("excludedKeywords", event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="js-excluded-companies" className="field__label">
            Leave out companies <span className="tag">Optional</span>
          </label>
          <input
            id="js-excluded-companies"
            className="input"
            value={draft.excludedCompanies}
            onChange={(event) => set("excludedCompanies", event.target.value)}
          />
        </div>
        <div className={`field${shown.maxResultsPerSource ? " is-invalid" : ""}`}>
          <label htmlFor="js-max" className="field__label">
            Results per source
          </label>
          <input
            id="js-max"
            className="input"
            inputMode="numeric"
            value={draft.maxResults}
            aria-invalid={shown.maxResultsPerSource ? true : undefined}
            aria-describedby={describedBy("js-max", null, shown.maxResultsPerSource)}
            onChange={(event) => set("maxResults", event.target.value)}
          />
          <FieldMessages id="js-max" error={shown.maxResultsPerSource} />
        </div>
      </details>

      <div className="search__actions">
        <button type="submit" className="button button--primary" disabled={busy !== null}>
          {busy === "search" ? "Starting…" : "Search"}
        </button>
        <button
          type="button"
          className="button button--secondary"
          disabled={busy !== null}
          onClick={() => submit("save")}
        >
          {busy === "save" ? "Saving…" : "Save preferences"}
        </button>
        <button
          type="button"
          className="text-button"
          onClick={() => {
            setDraft(toDraft(DEFAULT_PREFERENCES));
            setErrors({});
          }}
        >
          Reset to defaults
        </button>
      </div>
      <p className="field__hint">Searching reads public listings only. It never applies or contacts anyone.</p>
    </form>
  );
}
