"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { HttpJobsService } from "@/lib/jobs/http";
import { PreviewJobsService } from "@/lib/jobs/preview";
import {
  DEFAULT_PREFERENCES,
  type JobsService,
  type ListingView,
  type SearchPreferencesView,
  type SearchRunView,
  type SelectionChoice,
} from "@/lib/jobs/types";
import { asServiceError, type ServiceError } from "@/lib/service/errors";
import { writeHandoff } from "@/lib/handoff";
import { AppShell, PreviewStrip, type Connection } from "../shell/AppShell";
import { Modal } from "../Modal";
import { ApplyPrompt } from "../pipeline/ApplyPrompt";
import { SearchForm, type PreferencesInput } from "./SearchForm";
import { SourceStatus } from "./SourceStatus";
import { ListingCard, applicationUrlOf } from "./ListingCard";

type Filter = "all" | SelectionChoice | "undecided";

let previewJobs: PreviewJobsService | null = null;

export function JobsView({ mode }: { mode: "live" | "preview" }) {
  const service = useMemo<JobsService>(() => {
    if (mode === "live") return new HttpJobsService();
    previewJobs ??= new PreviewJobsService();
    return previewJobs;
  }, [mode]);
  const router = useRouter();
  const [prefs, setPrefs] = useState<SearchPreferencesView | null>(null);
  const [listings, setListings] = useState<ListingView[]>([]);
  const [run, setRun] = useState<SearchRunView | null>(null);
  const [loadError, setLoadError] = useState<ServiceError | null>(null);
  const [connection, setConnection] = useState<Connection>("checking");
  const [formBusy, setFormBusy] = useState<"search" | "save" | null>(null);
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState<{ id: string; kind: "decide" | "track" } | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [hideClosed, setHideClosed] = useState(true);
  const [applying, setApplying] = useState<ListingView | null>(null);
  const [formKey, setFormKey] = useState(0);
  const pollFailures = useRef(0);

  const load = useCallback(async () => {
    try {
      const [loadedPrefs, loaded] = await Promise.all([service.preferences(), service.listings()]);
      setPrefs(loadedPrefs);
      setFormKey((key) => key + 1);
      setListings(loaded.listings);
      setRun((current) => current ?? loaded.lastRun);
      setLoadError(null);
      setConnection("connected");
    } catch (error) {
      const serviceError = asServiceError(error);
      setLoadError(serviceError);
      setConnection(serviceError.code === "unavailable" ? "unavailable" : "connected");
    }
  }, [service]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll the running search until every source has finished.
  useEffect(() => {
    if (!run || run.finishedAt) return;
    let cancelled = false;
    const timer = window.setTimeout(
      async () => {
        try {
          const next = await service.searchStatus(run.id);
          if (cancelled) return;
          pollFailures.current = 0;
          setRun(next);
          if (next.finishedAt) {
            const loaded = await service.listings();
            if (!cancelled) setListings(loaded.listings);
          }
        } catch (error) {
          if (cancelled) return;
          pollFailures.current += 1;
          setNotice({
            tone: "error",
            text: `Lost contact with the search: ${asServiceError(error).message} Retrying.`,
          });
          setRun({ ...run });
        }
      },
      Math.min(10_000, (mode === "preview" ? 400 : 1500) * 2 ** pollFailures.current),
    );
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [run, service, mode]);

  async function search(input: PreferencesInput) {
    setFormBusy("search");
    setFormErrors({});
    setNotice(null);
    try {
      setPrefs(await service.savePreferences(input));
      setRun(await service.startSearch(input));
      setListings((await service.listings()).listings);
    } catch (error) {
      const serviceError = asServiceError(error);
      setFormErrors(serviceError.fieldErrors);
      setNotice({ tone: "error", text: serviceError.message });
    } finally {
      setFormBusy(null);
    }
  }

  async function save(input: PreferencesInput) {
    setFormBusy("save");
    setFormErrors({});
    setNotice(null);
    try {
      const saved = await service.savePreferences(input);
      setPrefs(saved);
      setListings((await service.listings()).listings);
      setNotice({
        tone: "ok",
        text: "Preferences saved. Decisions made with earlier preferences are marked as out of date.",
      });
    } catch (error) {
      const serviceError = asServiceError(error);
      setFormErrors(serviceError.fieldErrors);
      setNotice({ tone: "error", text: serviceError.message });
    } finally {
      setFormBusy(null);
    }
  }

  async function act(listing: ListingView, kind: "decide" | "track") {
    setBusy({ id: listing.id, kind });
    setNotice(null);
    try {
      const updated = kind === "decide" ? await service.decide(listing.id) : await service.track(listing.id);
      setListings((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      if (kind === "track")
        setNotice({ tone: "ok", text: `Added ${updated.company ?? updated.title} to your pipeline.` });
    } catch (error) {
      setNotice({ tone: "error", text: asServiceError(error).message });
    } finally {
      setBusy(null);
    }
  }

  function handoff(listing: ListingView, applicationUrl: string) {
    writeHandoff({
      applicationUrl,
      company: listing.company,
      role: listing.title,
      from: "jobs",
      pipelineEntryId: listing.pipelineEntryId,
      listingId: listing.id,
    });
    router.push(mode === "preview" ? "/preview" : "/");
  }

  const visible = listings.filter((listing) => {
    if (hideClosed && listing.status === "CLOSED") return false;
    if (filter === "all") return true;
    if (filter === "undecided") return !listing.selection;
    return listing.selection?.effectiveChoice === filter;
  });
  const counts = {
    all: listings.filter((item) => !hideClosed || item.status !== "CLOSED").length,
    APPLY: listings.filter(
      (item) => item.selection?.effectiveChoice === "APPLY" && (!hideClosed || item.status !== "CLOSED"),
    ).length,
    REVIEW: listings.filter(
      (item) => item.selection?.effectiveChoice === "REVIEW" && (!hideClosed || item.status !== "CLOSED"),
    ).length,
    SKIP: listings.filter(
      (item) => item.selection?.effectiveChoice === "SKIP" && (!hideClosed || item.status !== "CLOSED"),
    ).length,
    undecided: listings.filter((item) => !item.selection && (!hideClosed || item.status !== "CLOSED")).length,
  };
  const closedCount = listings.filter((item) => item.status === "CLOSED").length;

  return (
    <AppShell
      mode={mode}
      section="jobs"
      connection={mode === "preview" ? "connected" : connection}
      skipLabel="Skip to job search"
      previewBar={
        <PreviewStrip note="Fictional listings, sources and Jev decisions. No site is searched and no decision service is called." />
      }
      colophon="Searching and deciding never apply to a job. Applying always goes through the desk, one job at a time."
    >
      <header className="page-head">
        <h1 className="display">Jobs</h1>
        <p className="lede">
          Search your sources, see what each listing actually says, and ask Jev whether it&rsquo;s worth applying.
          Anything a listing doesn&rsquo;t state stays marked as unknown.
        </p>
      </header>

      {loadError && !prefs ? (
        <section className="notice notice--unavailable" aria-labelledby="jobs-unavailable">
          <h2 id="jobs-unavailable" className="notice__title">
            {loadError.code === "not_found"
              ? "Job search isn't available from the service yet"
              : "Job search couldn't be loaded"}
          </h2>
          <p>
            {loadError.code === "not_found"
              ? "The local service doesn't provide the search and selection routes yet, so no listings or decisions are shown."
              : loadError.message}
          </p>
          <div className="notice__actions">
            <button type="button" className="button button--secondary" onClick={() => void load()}>
              Check again
            </button>
            {mode === "live" && (
              <a className="text-link" href="/preview/jobs">
                See job search with fictional data
              </a>
            )}
          </div>
        </section>
      ) : !prefs ? (
        <p className="lede">Loading your search preferences…</p>
      ) : (
        <div className="jobs">
          <aside className="jobs__search">
            <SearchForm
              key={formKey}
              initial={prefs ?? { ...DEFAULT_PREFERENCES }}
              busy={formBusy}
              serverErrors={formErrors}
              onSearch={(input) => void search(input)}
              onSave={(input) => void save(input)}
            />
          </aside>

          <section className="jobs__results" aria-labelledby="results-title">
            {notice && (
              <p
                className={notice.tone === "ok" ? "notice-line" : "form-alert"}
                role={notice.tone === "ok" ? "status" : "alert"}
              >
                {notice.text}
              </p>
            )}
            {run && <SourceStatus run={run} />}

            <div className="results__head">
              <h2 id="results-title" className="results__title">
                Listings
              </h2>
              <fieldset className="segmented segmented--wrap">
                <legend className="visually-hidden">Show</legend>
                {(
                  [
                    ["all", "All"],
                    ["APPLY", "Apply"],
                    ["REVIEW", "Review"],
                    ["SKIP", "Skip"],
                    ["undecided", "Not decided"],
                  ] as const
                ).map(([value, label]) => (
                  <label key={value} className="segmented__option">
                    <input
                      type="radio"
                      name="choice-filter"
                      value={value}
                      checked={filter === value}
                      onChange={() => setFilter(value)}
                    />
                    <span>
                      {label} {counts[value]}
                    </span>
                  </label>
                ))}
              </fieldset>
              {closedCount > 0 && (
                <label className="check">
                  <input
                    type="checkbox"
                    checked={hideClosed}
                    onChange={(event) => setHideClosed(event.target.checked)}
                  />
                  <span>Hide {closedCount} closed</span>
                </label>
              )}
            </div>

            {listings.length === 0 ? (
              <p className="lede">No listings yet. Run a search to collect them.</p>
            ) : visible.length === 0 ? (
              <p className="lede">No listings match this filter.</p>
            ) : (
              <div className="listings">
                {visible.map((listing) => (
                  <ListingCard
                    key={listing.id}
                    listing={listing}
                    busy={busy?.id === listing.id ? busy.kind : null}
                    pipelineHref={mode === "preview" ? "/preview/pipeline" : "/pipeline"}
                    onDecide={() => void act(listing, "decide")}
                    onTrack={() => void act(listing, "track")}
                    onApply={() => setApplying(listing)}
                  />
                ))}
              </div>
            )}
          </section>
        </div>
      )}

      <Modal
        open={applying !== null}
        title={applying ? `Apply: ${[applying.company, applying.title].filter(Boolean).join(" — ")}` : "Apply"}
        onClose={() => setApplying(null)}
      >
        {applying && (
          <>
            {applying.selection && applying.selection.effectiveChoice !== "APPLY" && (
              <p className="form-alert">
                Jev&rsquo;s current decision is {applying.selection.effectiveChoice}. You can still apply; it&rsquo;s
                your choice.
              </p>
            )}
            <ApplyPrompt
              initialUrl={applicationUrlOf(applying)}
              savesTo={null}
              onSubmit={async (applicationUrl) => {
                const listing = applying;
                setApplying(null);
                handoff(listing, applicationUrl);
                return null;
              }}
            />
          </>
        )}
      </Modal>
    </AppShell>
  );
}
