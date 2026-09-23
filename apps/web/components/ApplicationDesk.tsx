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
import { AppShell, type Connection } from "./shell/AppShell";
import { takeHandoff, type DeskHandoff } from "@/lib/handoff";
import { executionProblem } from "@/lib/service/readiness";
import { useReadiness } from "./useReadiness";

const EMPTY_PROFILE: CandidateProfileInput = {
  firstName: "",
  lastName: "",
  email: "",
  phone: "",
  location: "",
  linkedinUrl: "",
  websiteUrl: "",
};

export type { Connection };

export interface DeskActions {
  answer(input: AnswerInput): Promise<boolean>;
  answerAndContinue(input: AnswerInput): Promise<boolean>;
  resume(): Promise<boolean>;
  reconcile(input: ReconcileInput): Promise<boolean>;
  checkNow(): void;
  startAnother(): void;
}

export function ApplicationDesk({ mode, initialScenario }: { mode: "live" | "preview"; initialScenario?: string }) {
  const { readiness, refresh: refreshReadiness } = useReadiness(mode);
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
  const [handoff, setHandoff] = useState<DeskHandoff | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [lostContact, setLostContact] = useState<string | null>(null);
  const [pollNonce, setPollNonce] = useState(0);
  const [restoreProblem, setRestoreProblem] = useState<Exclude<RestoreOutcome, { kind: "none" | "restored" }> | null>(
    null,
  );
  const failuresRef = useRef(0);
  // Bumped whenever this page stops following, or starts following something else,
  // so a restore that resolves afterwards is discarded instead of reviving it.
  const restoreGeneration = useRef(0);

  const noteUnavailable = useCallback((error: ServiceError) => {
    if (error.code === "unavailable") {
      setConnection("unavailable");
      setServiceMessage(error.message);
    }
  }, []);

  const loadCandidate = useCallback(async () => {
    const generation = ++restoreGeneration.current;
    const current = () => generation === restoreGeneration.current;
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
        // A restored or pending application only applies if it is still the one this page follows.
        const superseded =
          (outcome.kind === "restored" || outcome.kind === "pending") &&
          window.sessionStorage.getItem(ACTIVE_ID_KEY) !== outcome.applicationId;
        if (!current() || superseded) return;
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
      if (activeId && current()) {
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
    restoreGeneration.current += 1;
    window.sessionStorage.removeItem(ACTIVE_ID_KEY);
    setRestoreProblem(null);
  }

  useEffect(() => {
    void loadCandidate();
  }, [loadCandidate]);

  // A job chosen in the Pipeline or Jobs view only prefills the link.
  useEffect(() => {
    const incoming = takeHandoff();
    if (incoming) {
      setHandoff(incoming);
      setApplicationUrl(incoming.applicationUrl);
    }
  }, []);

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
      restoreGeneration.current += 1;
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
    if (mode === "live") {
      const problem = executionProblem(readiness, applicationUrl);
      if (problem) {
        setFormAlert(`Couldn't start the application. ${problem}`);
        return;
      }
    }

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
        return runAction(() => {
          const problem = mode === "live" ? executionProblem(readiness, view?.applicationUrl ?? "") : null;
          if (problem) throw new ServiceError("invalid", problem);
          return service.resume(viewId!);
        });
      },
      resume: () => runAction(() => {
        const problem = mode === "live" ? executionProblem(readiness, view?.applicationUrl ?? "") : null;
        if (problem) throw new ServiceError("invalid", problem);
        return service.resume(viewId!);
      }),
      reconcile: (input) => runAction(() => {
        const problem = mode === "live" && input.kind === "recheck" ? executionProblem(readiness, view?.applicationUrl ?? "") : null;
        if (problem) throw new ServiceError("invalid", problem);
        return service.reconcile(viewId!, input);
      }),
      checkNow: () => {
        failuresRef.current = 0;
        setPollNonce((nonce) => nonce + 1);
      },
      startAnother: () => {
        restoreGeneration.current += 1;
        setHandoff(null);
        if (service.mode === "live") window.sessionStorage.removeItem(ACTIVE_ID_KEY);
        setView(null);
        setActionError(null);
        setLostContact(null);
        setApplicationUrl("");
        setFormErrors({});
        setSubmitCount(0);
      },
    }),
    [runAction, service, viewId, view?.applicationUrl, mode, readiness],
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
    <AppShell
      mode={mode}
      section="desk"
      connection={connection}
      readiness={readiness}
      skipLabel="Skip to the application"
      previewBar={
        service instanceof PreviewApplicationService && (
          <PreviewBar initial={service.scenario} onChange={handleScenario} applicationOpen={view !== null} />
        )
      }
      colophon={
        mode === "preview"
          ? "Preview mode uses a fictional candidate and fictional job sites. Nothing is sent anywhere."
          : "Runs on this computer. Receipts and event history are kept by your local application service."
      }
    >
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
            <ServiceNotice mode={mode} message={serviceMessage} onRetry={async () => { await Promise.all([loadCandidate(), refreshReadiness()]); }} />
          )}
          {handoff && (
            <section className="notice notice--handoff" aria-labelledby="handoff-title">
              <h2 id="handoff-title" className="notice__title">
                {handoff.from === "jobs" ? "From your job search" : "From your pipeline"}
                {handoff.company || handoff.role
                  ? `: ${[handoff.company, handoff.role].filter(Boolean).join(" — ")}`
                  : ""}
              </h2>
              <p>
                The application link is filled in below. Nothing has been sent. Check your details and resume, then
                press <strong>Apply and submit</strong> if you want to apply.
              </p>
            </section>
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
            testMode={mode === "live"}
            onSubmit={handleApply}
          />
        </>
      )}
    </AppShell>
  );
}
