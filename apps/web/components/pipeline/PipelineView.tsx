"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { HttpPipelineService } from "@/lib/pipeline/http";
import { previewPipeline } from "@/lib/pipeline/previewStore";
import type { PipelineBoardView, PipelineEntryView, PipelineService } from "@/lib/pipeline/types";
import { compensationSummary } from "@/lib/pipeline/fields";
import { asServiceError, type ServiceError } from "@/lib/service/errors";
import { writeHandoff } from "@/lib/handoff";
import { AppShell, PreviewStrip, type Connection } from "../shell/AppShell";
import { Modal } from "../Modal";
import { EntryCard } from "./EntryCard";
import { EntryEditor, type EditorResult } from "./EntryEditor";
import { ImportPanel } from "./ImportPanel";
import { ApplyPrompt } from "./ApplyPrompt";
import { EntryBadges } from "./Badges";
import { formatShortDate } from "./dates";
import { useReadiness } from "../useReadiness";

type Dialog =
  | { kind: "edit"; entryId: string }
  | { kind: "create" }
  | { kind: "import" }
  | { kind: "apply"; entryId: string }
  | null;

export function PipelineView({ mode }: { mode: "live" | "preview" }) {
  const { readiness } = useReadiness(mode);
  const service = useMemo<PipelineService>(
    () => (mode === "preview" ? previewPipeline() : new HttpPipelineService()),
    [mode],
  );
  const router = useRouter();
  const [board, setBoard] = useState<PipelineBoardView | null>(null);
  const [loadError, setLoadError] = useState<ServiceError | null>(null);
  const [connection, setConnection] = useState<Connection>("checking");
  const [layout, setLayout] = useState<"board" | "list">("board");
  const [filter, setFilter] = useState("");
  const [focus, setFocus] = useState<"all" | "actions" | "interviews">("all");
  const [dialog, setDialog] = useState<Dialog>(null);
  const [editorKey, setEditorKey] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [moveError, setMoveError] = useState<string | null>(null);
  const [dropLane, setDropLane] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await service.board();
      setBoard(next);
      setLoadError(null);
      setConnection("connected");
      return next;
    } catch (error) {
      const serviceError = asServiceError(error);
      setLoadError(serviceError);
      setConnection(serviceError.code === "unavailable" ? "unavailable" : "connected");
      return null;
    }
  }, [service]);

  useEffect(() => {
    void load();
  }, [load]);

  const replace = (entry: PipelineEntryView) =>
    setBoard((current) =>
      current
        ? {
            ...current,
            entries: current.entries.some((item) => item.id === entry.id)
              ? current.entries.map((item) => (item.id === entry.id ? entry : item))
              : [...current.entries, entry],
          }
        : current,
    );

  const laneLabel = (id: string) => board?.lanes.find((lane) => lane.id === id)?.label ?? id;

  async function move(entry: PipelineEntryView, lane: string) {
    if (lane === entry.lane) return;
    setBusyId(entry.id);
    setMoveError(null);
    try {
      const moved = await service.move(entry.id, { revision: entry.revision, lane });
      replace(moved);
      setAnnouncement(`Moved ${entry.fields.company ?? entry.fields.role} to ${laneLabel(lane)}.`);
    } catch (error) {
      const serviceError = asServiceError(error);
      if (serviceError.code === "conflict") {
        const latest = await load();
        setMoveError(`${serviceError.message} ${latest ? "The board now shows the latest version; this move wasn't saved." : "This move wasn't saved, and the latest board couldn't be fetched. The cards below are the last loaded version."}`);
      } else {
        setMoveError(serviceError.message);
      }
    } finally {
      setBusyId(null);
    }
  }

  async function save(entry: PipelineEntryView | null, result: EditorResult): Promise<ServiceError | null> {
    try {
      if (!entry) {
        const created = await service.create({
          lane: result.lane,
          fields: result.fields,
          applicationUrl: result.applicationUrl,
        });
        replace(created);
        setAnnouncement(`Added ${created.fields.company ?? created.fields.role} to ${laneLabel(created.lane)}.`);
        setDialog(null);
        return null;
      }
      const changed = Object.fromEntries(
        Object.entries(result.fields).filter(
          ([key, value]) => entry.fields[key as keyof typeof entry.fields] !== value,
        ),
      );
      let current = entry;
      if (Object.keys(changed).length || result.applicationUrl !== entry.applicationUrl) {
        current = await service.update(entry.id, {
          revision: entry.revision,
          fields: changed,
          ...(result.applicationUrl !== entry.applicationUrl ? { applicationUrl: result.applicationUrl } : {}),
        });
        replace(current);
      }
      if (result.lane !== current.lane) {
        current = await service.move(current.id, { revision: current.revision, lane: result.lane });
        replace(current);
      }
      setAnnouncement(`Saved ${current.fields.company ?? current.fields.role}.`);
      setDialog(null);
      return null;
    } catch (error) {
      return asServiceError(error);
    }
  }

  function goApply(entry: PipelineEntryView, applicationUrl: string) {
    writeHandoff({
      applicationUrl,
      company: entry.fields.company,
      role: entry.fields.role,
      from: "pipeline",
      pipelineEntryId: entry.id,
      listingId: entry.listingId,
    });
    router.push(mode === "preview" ? "/preview" : "/");
  }

  const entries = board?.entries ?? [];
  const todayCT = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Chicago", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const upcoming = (entry: PipelineEntryView) => Boolean(entry.fields.nextInterviewDate && entry.fields.nextInterviewDate >= todayCT);
  const query = filter.trim().toLowerCase();
  const searched = query
    ? entries.filter((entry) =>
        [entry.fields.company, entry.fields.role, entry.fields.stage, entry.fields.status]
          .filter(Boolean)
          .some((value) => value!.toLowerCase().includes(query)),
      )
    : entries;
  const visible = searched.filter((entry) => focus === "all" || (focus === "actions" ? Boolean(entry.fields.nextAction) : upcoming(entry)));
  const editing =
    dialog?.kind === "edit" || dialog?.kind === "apply" ? entries.find((item) => item.id === dialog.entryId) : null;

  return (
    <AppShell
      mode={mode}
      section="pipeline"
      connection={mode === "preview" ? "connected" : connection}
      readiness={readiness}
      skipLabel="Skip to the pipeline"
      previewBar={
        <PreviewStrip note="Fictional companies and notes, shaped like the tracker workbook. Changes last until you reload." />
      }
      colophon="Your tracker. Moving or editing a card never applies, messages anyone or changes an application's state."
    >
      <header className="page-head">
        <p className="eyebrow">Keep the next step in sight</p>
        <h1 className="display">Pipeline</h1>
        <p className="lede">
          Track your conversations and keep the next step in sight.
        </p>
      </header>

      <p className="visually-hidden" role="status" aria-live="polite">
        {announcement}
      </p>

      {loadError && !board ? (
        <section className="notice notice--unavailable" aria-labelledby="pipeline-unavailable">
          <h2 id="pipeline-unavailable" className="notice__title">
            {loadError.code === "not_found"
              ? "The pipeline isn't available from the service yet"
              : "The pipeline couldn't be loaded"}
          </h2>
          <p>
            {loadError.code === "not_found"
              ? "The local service doesn't provide the pipeline routes yet. Nothing is shown rather than a made-up board."
              : loadError.message}
          </p>
          <div className="notice__actions">
            <button type="button" className="button button--secondary" onClick={() => void load()}>
              Check again
            </button>
            {mode === "live" && (
              <a className="text-link" href="/preview/pipeline">
                See the pipeline with fictional data
              </a>
            )}
          </div>
        </section>
      ) : !board ? (
        <p className="lede">Loading your pipeline…</p>
      ) : (
        <>
          {loadError && <section className="notice notice--unavailable" role="alert" aria-labelledby="pipeline-refresh-failed">
            <h2 id="pipeline-refresh-failed" className="notice__title">Showing the last loaded board</h2>
            <p>The latest changes couldn&rsquo;t be fetched. {loadError.message}</p>
            <button type="button" className="button button--secondary" onClick={async () => {
              if (await load()) setMoveError(null);
            }}>Refresh board</button>
          </section>}
          <div className="pipeline-focus" aria-label="Focus the pipeline">
            {([
              ["all", "All tracked", entries.length],
              ["actions", "With next actions", entries.filter((entry) => entry.fields.nextAction).length],
              ["interviews", "Upcoming interviews", entries.filter(upcoming).length],
            ] as const).map(([id, label, count]) => <button key={id} type="button" aria-pressed={focus === id} onClick={() => setFocus(id)}>
              <span className="pipeline-focus__count">{count}</span><span>{label}</span><span className="pipeline-focus__arrow" aria-hidden="true">↗</span>
            </button>)}
            <p>Your tracking stages. Confirmed applications keep a separate receipt.</p>
          </div>
          <div className="toolbar">
            <fieldset className="segmented">
              <legend className="visually-hidden">Layout</legend>
              {(["board", "list"] as const).map((value) => (
                <label key={value} className="segmented__option">
                  <input
                    type="radio"
                    name="layout"
                    value={value}
                    checked={layout === value}
                    onChange={() => setLayout(value)}
                  />
                  <span>{value === "board" ? "Board" : "List"}</span>
                </label>
              ))}
            </fieldset>
            <div className="toolbar__filter">
              <label htmlFor="pipeline-filter" className="visually-hidden">
                Filter by company, role, stage or status
              </label>
              <input
                id="pipeline-filter"
                className="input"
                type="search"
                placeholder="Filter by company, role or status"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
              />
            </div>
            <div className="toolbar__actions">
              <button
                type="button"
                className="button button--secondary"
                onClick={() => {
                  setEditorKey((key) => key + 1);
                  setDialog({ kind: "create" });
                }}
              >
                Track a job
              </button>
              <button type="button" className="button button--secondary" onClick={() => setDialog({ kind: "import" })}>
                Import
              </button>
            </div>
          </div>

          {moveError && (
            <p className="form-alert" role="alert">
              {moveError}
            </p>
          )}
          {(query || focus !== "all") && (
            <p className="field__hint" role="status">
              Showing {visible.length} of {entries.length}.
            </p>
          )}

          {visible.length === 0 && (query || focus !== "all") && <p className="empty-note">No roles match this view. Choose All tracked or clear the search to see the rest.</p>}
          {layout === "board" ? (
            <div className="board">
              {board.lanes.map((lane) => {
                const cards = visible.filter((entry) => entry.lane === lane.id);
                return (
                  <section
                    key={lane.id}
                    className={`lane${dropLane === lane.id ? " is-drop" : ""}`}
                    aria-labelledby={`lane-${lane.id}`}
                    onDragOver={(event) => {
                      if (event.dataTransfer.types.includes("text/x-imx-entry")) {
                        event.preventDefault();
                        setDropLane(lane.id);
                      }
                    }}
                    onDragLeave={() => setDropLane((current) => (current === lane.id ? null : current))}
                    onDrop={(event) => {
                      event.preventDefault();
                      setDropLane(null);
                      const entry = entries.find((item) => item.id === event.dataTransfer.getData("text/x-imx-entry"));
                      if (entry) void move(entry, lane.id);
                    }}
                  >
                    <h2 id={`lane-${lane.id}`} className="lane__title">
                      {lane.label} <span className="lane__count">{cards.length}</span>
                    </h2>
                    <div className="lane__cards">
                      {cards.length === 0 ? (
                        <p className="lane__empty">Nothing here.</p>
                      ) : (
                        cards.map((entry) => (
                          <EntryCard
                            key={`${entry.id}-${entry.revision}`}
                            entry={entry}
                            lanes={board.lanes}
                            busy={busyId === entry.id}
                            onOpen={() => {
                              setEditorKey((key) => key + 1);
                              setDialog({ kind: "edit", entryId: entry.id });
                            }}
                            onMove={(target) => void move(entry, target)}
                            onApply={() => setDialog({ kind: "apply", entryId: entry.id })}
                          />
                        ))
                      )}
                    </div>
                  </section>
                );
              })}
            </div>
          ) : (
            <div className="table-scroll">
              <table className="pipeline-table">
                <caption className="visually-hidden">Pipeline entries</caption>
                <thead>
                  <tr>
                    <th scope="col">Company and role</th>
                    <th scope="col">Lane</th>
                    <th scope="col">Stage and status</th>
                    <th scope="col">Priority</th>
                    <th scope="col">Fit</th>
                    <th scope="col">Pay</th>
                    <th scope="col">Next action</th>
                    <th scope="col">
                      <span className="visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((entry) => (
                    <tr key={entry.id}>
                      <th scope="row" data-label="Company and role">
                        <span className="card__company">{entry.fields.company ?? "—"}</span>
                        <span className="card__role">{entry.fields.role ?? ""}</span>
                        <EntryBadges entry={entry} />
                      </th>
                      <td data-label="Lane">{laneLabel(entry.lane)}</td>
                      <td data-label="Stage and status">
                        {[entry.fields.stage, entry.fields.status].filter(Boolean).join(" — ") || "—"}
                      </td>
                      <td data-label="Priority">{entry.fields.priority ?? "—"}</td>
                      <td data-label="Fit">{entry.fields.fitScore !== null ? `${entry.fields.fitScore}/10` : "—"}</td>
                      <td data-label="Pay">{compensationSummary(entry.fields) ?? "—"}</td>
                      <td data-label="Next action">
                        {entry.fields.nextAction ?? "—"}
                        {entry.fields.suggestedFollowUpDate && (
                          <span className="card__due">
                            {" "}
                            Follow-up {formatShortDate(entry.fields.suggestedFollowUpDate)}
                          </span>
                        )}
                      </td>
                      <td data-label="Actions">
                        <button
                          type="button"
                          className="chip-button"
                          onClick={() => {
                            setEditorKey((key) => key + 1);
                            setDialog({ kind: "edit", entryId: entry.id });
                          }}
                        >
                          Open<span className="visually-hidden"> {entry.fields.company ?? entry.fields.role}</span>
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <Modal
        open={dialog?.kind === "edit" || dialog?.kind === "create"}
        wide
        title={
          dialog?.kind === "create"
            ? "Track a job"
            : editing
              ? [editing.fields.company, editing.fields.role].filter(Boolean).join(" — ") || "Edit card"
              : "Edit card"
        }
        onClose={() => setDialog(null)}
      >
        {board && (dialog?.kind === "create" || (dialog?.kind === "edit" && editing)) && (
          <EntryEditor
            key={editorKey}
            entry={dialog.kind === "edit" ? editing! : null}
            lanes={board.lanes}
            defaultLane={board.lanes[0]?.id ?? "saved"}
            onSave={(result) => save(dialog.kind === "edit" ? editing! : null, result)}
            onReload={async () => {
              await load();
              setEditorKey((key) => key + 1);
            }}
          />
        )}
      </Modal>

      <Modal open={dialog?.kind === "import"} wide title="Import tracker rows" onClose={() => setDialog(null)}>
        <ImportPanel service={service} onImported={() => void load()} />
      </Modal>

      <Modal
        open={dialog?.kind === "apply"}
        title={
          editing ? `Apply: ${[editing.fields.company, editing.fields.role].filter(Boolean).join(" — ")}` : "Apply"
        }
        onClose={() => setDialog(null)}
      >
        {editing && dialog?.kind === "apply" && (
          <ApplyPrompt
            initialUrl={editing.applicationUrl}
            savesTo="the card"
            onSubmit={async (applicationUrl) => {
              if (applicationUrl !== editing.applicationUrl) {
                try {
                  replace(await service.update(editing.id, { revision: editing.revision, applicationUrl }));
                } catch (error) {
                  return asServiceError(error).message;
                }
              }
              setDialog(null);
              goApply(editing, applicationUrl);
              return null;
            }}
          />
        )}
      </Modal>
    </AppShell>
  );
}
