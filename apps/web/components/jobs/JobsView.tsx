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
import { rankListings, tierHeading } from "@/lib/jobs/ranking";
import { useReadiness } from "../useReadiness";

type Filter = "all" | SelectionChoice | "undecided";
type PendingDecision = { previous: string | null; taskId: string | null; requestedAt: number };

let previewJobs: PreviewJobsService | null = null;

export function JobsView({ mode }: { mode: "live" | "preview" }) {
  const { readiness } = useReadiness(mode);
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
  const [busy, setBusy] = useState<Record<string, "decide" | "track">>({});
  const [filter, setFilter] = useState<Filter>("all");
  const [hideClosed, setHideClosed] = useState(true);
  const [applying, setApplying] = useState<ListingView | null>(null);
  const [formKey, setFormKey] = useState(0);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const [pendingDecisions, setPendingDecisions] = useState<Record<string, PendingDecision>>({});
  const [decisionPoll, setDecisionPoll] = useState(0);
  const pollFailures = useRef(0);

  const recoverDecisions = useCallback((items: ListingView[]) => {
    setPendingDecisions((current) => {
      const next = { ...current };
      for (const item of items) {
        const task = item.decisionTask;
        if (task?.state === "QUEUED" || task?.state === "RUNNING") {
          next[item.id] = {
            previous: item.selection?.id ?? null,
            taskId: task.id,
            requestedAt: current[item.id]?.taskId === task.id ? current[item.id].requestedAt : Date.now(),
          };
        } else if (task && current[item.id]?.taskId === task.id) delete next[item.id];
      }
      if (mode === "live") window.sessionStorage.setItem("imx.pending-decisions", JSON.stringify(next));
      return next;
    });
  }, [mode]);

  const load = useCallback(async () => {
    try {
      const [loadedPrefs, loaded] = await Promise.all([service.preferences(), service.listings()]);
      setPrefs(loadedPrefs);
      setFormKey((key) => key + 1);
      setListings(loaded.listings);
      recoverDecisions(loaded.listings);
      setRun((current) => current ?? loaded.lastRun);
      setLoadError(null);
      setConnection("connected");
    } catch (error) {
      const serviceError = asServiceError(error);
      setLoadError(serviceError);
      setConnection(serviceError.code === "unavailable" ? "unavailable" : "connected");
    }
  }, [service, recoverDecisions]);

  useEffect(() => {
    void load();
  }, [load]);

  // A 202 means the service accepted the decision request, not that Jev finished.
  // Keep watching by GET after reload; never re-POST merely to refresh progress.
  useEffect(() => {
    if (mode !== "live") return;
    try {
      const saved = JSON.parse(window.sessionStorage.getItem("imx.pending-decisions") ?? "{}");
      if (saved && typeof saved === "object" && !Array.isArray(saved)) {
        setPendingDecisions(Object.fromEntries(Object.entries(saved).filter(([, value]) => value && typeof value === "object" && typeof (value as PendingDecision).requestedAt === "number")) as Record<string, PendingDecision>);
      }
    } catch { /* No pending request can be recovered from a damaged local marker. */ }
  }, [mode]);

  useEffect(() => {
    if (Object.keys(pendingDecisions).length === 0) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      const results = await Promise.allSettled(Object.entries(pendingDecisions).map(async ([id, pending]) => {
        const next = await service.listing(id);
        const task = next.decisionTask;
        const applies = task && (!pending.taskId || task.id === pending.taskId);
        const failed = applies && (task.state === "FAILED" || task.state === "INTERRUPTED");
        const completed = applies && task.state === "DONE";
        const timedOut = Date.now() - pending.requestedAt > 120_000;
        const error = failed ? task.error || "The decision stopped before it completed. You can ask Jev again."
          : timedOut && !completed ? "This decision is taking longer than expected. Refresh listings to check its status; another request is not needed." : null;
        return { id, next, error, done: Boolean(failed || completed || timedOut || (!task && next.selection && !next.selection.stale && next.selection.id !== pending.previous)) };
      }));
      if (cancelled) return;
      for (const result of results) {
        if (result.status === "fulfilled") {
          const { id, next, done, error } = result.value;
          setListings((items) => items.map((item) => item.id === id ? next : item));
          if (error) setNotice({ tone: "error", text: error });
          if (done) setPendingDecisions((current) => {
            const remaining = { ...current };
            delete remaining[id];
            if (mode === "live") window.sessionStorage.setItem("imx.pending-decisions", JSON.stringify(remaining));
            return remaining;
          });
        } else {
          const expired = Object.values(pendingDecisions).some((pending) => Date.now() - pending.requestedAt > 120_000);
          setNotice({ tone: "error", text: `${asServiceError(result.reason).message} ${expired ? "Refresh listings to check the decision's status; another request is not needed." : "The decision is still pending. Checking again shortly."}` });
        }
      }
      if (Object.values(pendingDecisions).some((pending) => Date.now() - pending.requestedAt > 120_000)) {
        setPendingDecisions((current) => {
          const remaining = Object.fromEntries(Object.entries(current).filter(([, pending]) => Date.now() - pending.requestedAt <= 120_000));
          if (mode === "live") window.sessionStorage.setItem("imx.pending-decisions", JSON.stringify(remaining));
          return remaining;
        });
      }
      setDecisionPoll((current) => current + 1);
    }, 1800);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [pendingDecisions, decisionPoll, service, mode]);

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
      setPreferencesOpen(false);
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
      setPreferencesOpen(false);
    } catch (error) {
      const serviceError = asServiceError(error);
      setFormErrors(serviceError.fieldErrors);
      setNotice({ tone: "error", text: serviceError.message });
    } finally {
      setFormBusy(null);
    }
  }

  async function act(listing: ListingView, kind: "decide" | "track") {
    if (busy[listing.id] || Object.prototype.hasOwnProperty.call(pendingDecisions, listing.id)) return;
    setBusy((current) => ({ ...current, [listing.id]: kind }));
    setNotice(null);
    try {
      const updated = kind === "decide" ? await service.decide(listing.id) : await service.track(listing.id);
      setListings((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      if (kind === "decide" && updated.decisionPending) {
        setPendingDecisions((current) => {
          const next = { ...current, [listing.id]: { previous: updated.selection?.id ?? null, taskId: updated.decisionTask?.id ?? null, requestedAt: Date.now() } };
          if (mode === "live") window.sessionStorage.setItem("imx.pending-decisions", JSON.stringify(next));
          return next;
        });
      }
      if (kind === "track")
        setNotice({ tone: "ok", text: `Added ${updated.company ?? updated.title} to your pipeline.` });
    } catch (error) {
      setNotice({ tone: "error", text: asServiceError(error).message });
    } finally {
      setBusy((current) => {
        const next = { ...current };
        delete next[listing.id];
        return next;
      });
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
  const ranking = {
    onsite: prefs?.onsite ?? DEFAULT_PREFERENCES.onsite,
    remote: prefs ? prefs.remote : DEFAULT_PREFERENCES.remote,
    locationPriority: prefs?.locationPriority ?? DEFAULT_PREFERENCES.locationPriority,
  };

  return (
    <AppShell
      mode={mode}
      section="jobs"
      connection={mode === "preview" ? "connected" : connection}
      readiness={readiness}
      skipLabel="Skip to job search"
      previewBar={
        <PreviewStrip note="Fictional listings, sources and Jev decisions. No site is searched and no decision service is called." />
      }
      colophon="Searching and deciding never apply to a job. Applying always goes through the desk, one job at a time."
    >
      <header className="page-head">
        <p className="eyebrow">Discover opportunities</p>
        <h1 className="display">Jobs</h1>
        <p className="lede">
          Roles matched to your experience. Compare the facts, then choose your next move.
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
          <section className="search-brief" aria-label="Current search preferences">
            <div className="search-brief__scope">
              <span className="eyebrow">Role focus</span>
              <strong>{prefs.roleFocus?.split(":")[0] || "Performance marketing operator"}</strong>
              <span>{prefs.titlePhrases.length} search seeds · matched by responsibilities</span>
            </div>
            <div>
              <span className="eyebrow">Location</span>
              <strong>{prefs.onsite[0]?.location ?? "Remote"}{prefs.locationPriority === "STRONGLY_PREFER_ONSITE_HYBRID" && prefs.onsite.length > 0 ? " first" : ""}</strong>
              <span>{prefs.remote ? `Remote in ${prefs.remote.eligibleRegion} included` : "Onsite and hybrid targets"}</span>
            </div>
            <div>
              <span className="eyebrow">Minimum pay</span>
              <strong>{prefs.minimumCompensation ? `${prefs.minimumCompensation.currency === "USD" ? "$" : `${prefs.minimumCompensation.currency} `}${prefs.minimumCompensation.amount.toLocaleString("en-US")} / ${prefs.minimumCompensation.period.toLowerCase()}` : "No minimum"}</strong>
              <span>Unstated pay stays unresolved</span>
            </div>
            <div className="search-brief__actions">
              <button type="button" className="button button--secondary" onClick={() => setPreferencesOpen(true)}>Edit preferences</button>
              <button type="button" className="button button--primary" disabled={formBusy !== null || Boolean(run && !run.finishedAt)} onClick={() => {
                const { fingerprint: _fingerprint, ...input } = prefs;
                void search(input);
              }}>
                {formBusy === "search" ? "Starting…" : run && !run.finishedAt ? "Searching…" : "Search"}
                <span aria-hidden="true">↗</span>
              </button>
            </div>
          </section>

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
              <button type="button" className="text-button results__refresh" onClick={() => void load()}>Refresh listings</button>
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
                {rankListings(visible, ranking).map((group, index) => {
                  const heading = tierHeading(group.tiers, ranking);
                  return (
                    <section key={group.tiers.join("+")} className="tier" aria-label={heading}>
                      <h3 className="tier__title">
                        <span className="tier__rank">{String(index + 1).padStart(2, "0")}</span>
                        {heading}
                        <span className="tier__note">
                          {group.listings.length} {group.listings.length === 1 ? "listing" : "listings"}
                        </span>
                      </h3>
                      {group.listings.map((listing) => (
                        <ListingCard
                          key={listing.id}
                          listing={listing}
                          tierLabel={heading}
                          tierUnknown={group.tiers.includes("UNRESOLVED") || group.tiers.includes("REMOTE_UNCONFIRMED")}
                          busy={Object.prototype.hasOwnProperty.call(pendingDecisions, listing.id) || listing.decisionTask?.state === "RUNNING" || listing.decisionTask?.state === "QUEUED" ? "decide" : busy[listing.id] ?? null}
                          pipelineHref={mode === "preview" ? "/preview/pipeline" : "/pipeline"}
                          onDecide={() => void act(listing, "decide")}
                          onTrack={() => void act(listing, "track")}
                          onApply={() => setApplying(listing)}
                        />
                      ))}
                    </section>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      )}

      <Modal open={preferencesOpen} wide title="Search preferences" onClose={() => setPreferencesOpen(false)}>
        {prefs && <SearchForm
          key={formKey}
          initial={prefs}
          busy={formBusy}
          serverErrors={formErrors}
          onSearch={(input) => void search(input)}
          onSave={(input) => void save(input)}
        />}
      </Modal>

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
