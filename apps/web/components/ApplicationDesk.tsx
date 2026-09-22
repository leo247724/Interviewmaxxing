"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { HttpApplicationService } from "@/lib/service/http";
import { PreviewApplicationService, isPreviewScenario, type PreviewScenarioId } from "@/lib/service/preview";
import { ServiceError, asServiceError } from "@/lib/service/errors";
import type {
  AnswerInput,
  ApplicationService,
  ApplicationView,
  CandidateProfileInput,
  ReconcileInput,
  ResumeDocumentView,
} from "@/lib/service/types";
import { isActive } from "@/lib/state";
import { ACTIVE_ID_KEY, restoreActiveApplication, type RestoreOutcome } from "@/lib/restore";
import { validateApplyForm, type FieldErrors } from "@/lib/validation";
import { ComposeForm } from "./ComposeForm";
import { ApplicationWorkspace } from "./ApplicationWorkspace";
import { PreviewBar } from "./PreviewBar";
import { ServiceNotice } from "./ServiceNotice";
import { RestoreNotice } from "./RestoreNotice";

const EMPTY_PROFILE: CandidateProfileInput = {
  firstName: "",
  lastName: "",
  email: "",
  phone: "",
  location: "",
  linkedinUrl: "",
  websiteUrl: "",
};

export type Connection = "checking" | "connected" | "unavailable";

export interface DeskActions {
  answer(input: AnswerInput): Promise<boolean>;
  answerAndContinue(input: AnswerInput): Promise<boolean>;
  resume(): Promise<boolean>;
  reconcile(input: ReconcileInput): Promise<boolean>;
  checkNow(): void;
  startAnother(): void;
}

export function ApplicationDesk({ mode, initialScenario }: { mode: "live" | "preview"; initialScenario?: string }) {
  const service = useMemo<ApplicationService>(
    () =>
      mode === "preview"
        ? new PreviewApplicationService({
            scenario: isPreviewScenario(initialScenario) ? initialScenario : "straight",
          })
        : new HttpApplicationService(),
    [mode, initialScenario],
  );
  const pollInterval = mode === "preview" ? 500 : 1500;

  const [connection, setConnection] = useState<Connection>("checking");
  const [serviceMessage, setServiceMessage] = useState<string | null>(null);
  const [candidateLoaded, setCandidateLoaded] = useState(false);

  const [applicationUrl, setApplicationUrl] = useState("");
  const [profile, setProfile] = useState<CandidateProfileInput>(EMPTY_PROFILE);
  const [resumes, setResumes] = useState<ResumeDocumentView[]>([]);
  const [resumeId, setResumeId] = useState<string | null>(null);
  const [formErrors, setFormErrors] = useState<FieldErrors>({});
  const [formAlert, setFormAlert] = useState<string | null>(null);
  const [submitCount, setSubmitCount] = useState(0);
  const [starting, setStarting] = useState(false);
  const [uploading, setUploading] = useState(false);

  const [view, setView] = useState<ApplicationView | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [lostContact, setLostContact] = useState<string | null>(null);
  const [pollNonce, setPollNonce] = useState(0);
  const [restoreProblem, setRestoreProblem] = useState<Exclude<RestoreOutcome, { kind: "none" | "restored" }> | null>(
    null,
  );
  const failuresRef = useRef(0);

  const noteUnavailable = useCallback((error: ServiceError) => {
    if (error.code === "unavailable") {
      setConnection("unavailable");
      setServiceMessage(error.message);
    }
  }, []);

  const loadCandidate = useCallback(async () => {
    setConnection("checking");
    try {
      const candidate = await service.getCandidate();
      setConnection("connected");
      setServiceMessage(null);
      setResumes(candidate.resumes);
      // Only fill blanks: never overwrite what the user has typed.
      setProfile((current) => {
        const merged = { ...current };
        for (const key of Object.keys(merged) as (keyof CandidateProfileInput)[]) {
          if (!merged[key].trim()) merged[key] = candidate.profile[key] ?? "";
        }
        return merged;
      });
      setResumeId((current) => current ?? candidate.defaultResumeId ?? candidate.resumes[0]?.id ?? null);
      setCandidateLoaded(true);

      if (service.mode === "live") {
        const outcome = await restoreActiveApplication(service, window.sessionStorage);
        if (outcome.kind === "restored") {
          setView(outcome.view);
          setRestoreProblem(null);
        } else if (outcome.kind === "none") {
          setRestoreProblem(null);
        } else {
          setRestoreProblem(outcome);
          if (outcome.kind === "pending" && outcome.code === "unavailable") {
            setConnection("unavailable");
            setServiceMessage(outcome.message);
          }
        }
      }
    } catch (error) {
      const serviceError = asServiceError(error);
      setConnection(serviceError.code === "unavailable" ? "unavailable" : "connected");
      setServiceMessage(serviceError.message);
      // Keep following a saved application even when the service can't be reached yet.
      const activeId = service.mode === "live" ? window.sessionStorage.getItem(ACTIVE_ID_KEY) : null;
      if (activeId) {
        setRestoreProblem({
          kind: "pending",
          applicationId: activeId,
          code: serviceError.code,
          message: serviceError.message,
        });
      }
    }
  }, [service]);

  function stopFollowing() {
    window.sessionStorage.removeItem(ACTIVE_ID_KEY);
    setRestoreProblem(null);
  }

  useEffect(() => {
    void loadCandidate();
  }, [loadCandidate]);

  // Poll while the service is working on the application.
  useEffect(() => {
    if (!view || !isActive(view.state)) return;
    let cancelled = false;
    const delay = failuresRef.current === 0 ? pollInterval : Math.min(10_000, pollInterval * 2 ** failuresRef.current);
    const timer = window.setTimeout(async () => {
      try {
        const next = await service.status(view.id);
        if (cancelled) return;
        failuresRef.current = 0;
        setLostContact(null);
        setConnection("connected");
        setView(next);
      } catch (error) {
        if (cancelled) return;
        failuresRef.current += 1;
        setLostContact(asServiceError(error).message);
        setPollNonce((nonce) => nonce + 1);
      }
    }, delay);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [view, service, pollInterval, pollNonce]);

  const adopt = useCallback(
    (next: ApplicationView) => {
      setView(next);
      setActionError(null);
      setRestoreProblem(null);
      if (service.mode === "live") window.sessionStorage.setItem(ACTIVE_ID_KEY, next.id);
    },
    [service],
  );

  function clearErrors(keys: string[]) {
    if (!keys.some((key) => key in formErrors)) return;
    setFormErrors((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !keys.includes(key))));
  }

  async function handleApply() {
    setFormAlert(null);
    const errors = validateApplyForm({ applicationUrl, profile, resumeId });
    setFormErrors(errors);
    setSubmitCount((count) => count + 1);
    if (Object.keys(errors).length > 0 || !resumeId) return;

    setStarting(true);
    try {
      const started = await service.start({ applicationUrl: applicationUrl.trim(), profile, resumeId });
      setConnection("connected");
      failuresRef.current = 0;
      setLostContact(null);
      adopt(started);
    } catch (error) {
      const serviceError = asServiceError(error);
      noteUnavailable(serviceError);
      if (Object.keys(serviceError.fieldErrors).length > 0) {
        setFormErrors(serviceError.fieldErrors);
        setSubmitCount((count) => count + 1);
      } else {
        setFormAlert(
          serviceError.code === "unavailable"
            ? `Couldn't start the application. ${serviceError.message}`
            : serviceError.message,
        );
      }
    } finally {
      setStarting(false);
    }
  }

  async function handleUpload(file: File) {
    setUploading(true);
    clearErrors(["resumeId", "resumeFile"]);
    try {
      const uploaded = await service.uploadResume(file);
      setResumes((current) => [uploaded, ...current.filter((item) => item.id !== uploaded.id)]);
      setResumeId(uploaded.id);
      return true;
    } catch (error) {
      const serviceError = asServiceError(error);
      noteUnavailable(serviceError);
      setFormErrors((current) => ({
        ...current,
        resumeFile:
          serviceError.fieldErrors.resumeFile ??
          (serviceError.code === "unavailable"
            ? `The resume wasn't uploaded. ${serviceError.message}`
            : serviceError.message),
      }));
      return false;
    } finally {
      setUploading(false);
    }
  }

  const runAction = useCallback(
    async (action: () => Promise<ApplicationView>) => {
      setActionError(null);
      try {
        adopt(await action());
        failuresRef.current = 0;
        setLostContact(null);
        return true;
      } catch (error) {
        const serviceError = asServiceError(error);
        noteUnavailable(serviceError);
        setActionError(serviceError.message);
        return false;
      }
    },
    [adopt, noteUnavailable],
  );

  const viewId = view?.id;
  const actions: DeskActions = useMemo(
    () => ({
      answer: (input) => runAction(() => service.answer(viewId!, input)),
      answerAndContinue: async (input) => {
        let rejected = false;
        const saved = await runAction(async () => {
          const next = await service.answer(viewId!, input);
          rejected = next.needs?.kind === "questions" && Object.keys(next.needs.errors).length > 0;
          return next;
        });
        if (!saved) return false;
        if (rejected) {
          setActionError("The site didn't accept some answers. They're marked below; nothing was submitted.");
          return false;
        }
        return runAction(() => service.resume(viewId!));
      },
      resume: () => runAction(() => service.resume(viewId!)),
      reconcile: (input) => runAction(() => service.reconcile(viewId!, input)),
      checkNow: () => {
        failuresRef.current = 0;
        setPollNonce((nonce) => nonce + 1);
      },
      startAnother: () => {
        if (service.mode === "live") window.sessionStorage.removeItem(ACTIVE_ID_KEY);
        setView(null);
        setActionError(null);
        setLostContact(null);
        setApplicationUrl("");
        setFormErrors({});
        setSubmitCount(0);
      },
    }),
    [runAction, service, viewId],
  );

  function handleScenario(scenario: PreviewScenarioId) {
    if (service instanceof PreviewApplicationService) {
      service.scenario = scenario;
      const url = new URL(window.location.href);
      url.searchParams.set("scenario", scenario);
      window.history.replaceState(null, "", url);
      if (!view) void loadCandidate();
    }
  }

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to the application
      </a>
      {service instanceof PreviewApplicationService && (
        <PreviewBar initial={service.scenario} onChange={handleScenario} applicationOpen={view !== null} />
      )}
      <div className="desk">
        <header className="masthead">
          <div className="masthead__brand">
            <span className="wordmark">Interviewmaxxing</span>
            <span className="masthead__desk">Application desk</span>
          </div>
          <ConnectionBadge mode={mode} connection={connection} />
        </header>

        <main id="main" className="desk__main" tabIndex={-1}>
          {view ? (
            <ApplicationWorkspace
              view={view}
              mode={mode}
              profile={profile}
              actions={actions}
              actionError={actionError}
              lostContact={lostContact}
            />
          ) : (
            <>
              {restoreProblem && (
                <RestoreNotice
                  problem={restoreProblem}
                  onRetry={loadCandidate}
                  onStopFollowing={stopFollowing}
                  onDismiss={() => setRestoreProblem(null)}
                />
              )}
              {connection === "unavailable" && (
                <ServiceNotice mode={mode} message={serviceMessage} onRetry={loadCandidate} />
              )}
              <ComposeForm
                applicationUrl={applicationUrl}
                onApplicationUrl={(value) => {
                  clearErrors(["applicationUrl"]);
                  setApplicationUrl(value);
                }}
                profile={profile}
                onProfile={(next) => {
                  clearErrors(
                    (Object.keys(next) as (keyof CandidateProfileInput)[]).filter((key) => next[key] !== profile[key]),
                  );
                  setProfile(next);
                }}
                resumes={resumes}
                resumeId={resumeId}
                onResumeId={(id) => {
                  clearErrors(["resumeId", "resumeFile"]);
                  setResumeId(id);
                }}
                onUpload={handleUpload}
                uploading={uploading}
                errors={formErrors}
                submitCount={submitCount}
                alert={formAlert}
                starting={starting}
                candidateLoaded={candidateLoaded || connection !== "checking"}
                onSubmit={handleApply}
              />
            </>
          )}
        </main>

        <footer className="colophon">
          <p>
            {mode === "preview"
              ? "Preview mode uses a fictional candidate and fictional job sites. Nothing is sent anywhere."
              : "Runs on this computer. Receipts and event history are kept by your local application service."}
          </p>
        </footer>
      </div>
    </>
  );
}

function ConnectionBadge({ mode, connection }: { mode: "live" | "preview"; connection: Connection }) {
  if (mode === "preview") {
    return (
      <p className="badge badge--preview">
        <span className="badge__dot" aria-hidden="true" />
        Preview · nothing is sent
      </p>
    );
  }
  const label =
    connection === "connected"
      ? "Service connected"
      : connection === "checking"
        ? "Checking service…"
        : "Service not connected";
  return (
    <p className={`badge badge--${connection}`} role="status">
      <span className="badge__dot" aria-hidden="true" />
      {label}
    </p>
  );
}
