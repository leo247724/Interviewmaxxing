export interface InterviewSetup {
  company: string; title: string; companyUrl: string; jobUrl: string; jobDescription: string;
  round: number; persona: string; durationMinutes: number; targetScore: number;
  resumeText: string; linkedinText: string; notes: string; previousSessionId?: string;
}
export interface InterviewScore {
  overallScore?: number; dimensions?: Record<string, number>; confidence?: Record<string, number>;
  evidence?: unknown; rubricVersion?: string; model?: string;
  weaknesses?: string[]; actionItems?: string[]; followUp?: string;
  gradingSource?: string; feedbackSource?: string; feedbackWarning?: string;
}
export interface InterviewTurn {
  id: string; requestId: string; question: string; answer: string;
  judgingAt?: string; scoreStatus: 'pending' | 'completed' | 'failed'; score?: InterviewScore | null; error?: string | null;
  retrieval?: unknown;
}
export interface InterviewDocument { id: string; kind: string; name: string; status: string; chunkCount: number; error?: string | null; sourceUrl?: string }
export interface InterviewReport {
  overallScore?: number | null; dimensions?: Record<string, number>; gradedAnswers: number;
  totalAnswers: number; coverage?: unknown; weakDimensions?: string[]; nextDrills?: string[];
  scoreChange?: number | null; rubricVersion?: string; unansweredIssues?: string[]; drillProvenance?: string;
}
export interface InterviewSession extends InterviewSetup {
  id: string; status: 'draft' | 'active' | 'completed'; createdAt: string;
  startedAt?: string | null; deadlineAt?: string | null; endedAt?: string | null;
  currentQuestion?: string; questionError?: string | null; documents: InterviewDocument[]; turns: InterviewTurn[];
  sourceIngestion?: { status: 'processing' | 'ready' | 'failed'; error?: string | null };
  report?: InterviewReport | null; contextWarnings: string[];
}
export interface InterviewIndex { readiness: Record<string, unknown>; sessions: InterviewSession[] }
export interface InterviewContext { resumeText: string; candidateSummary?: string; warnings?: string[]; jobs: Array<{id: string; company: string; title: string; companyUrl?: string; jobUrl: string; jobDescription: string; notes?: string}> }
export const DEFAULT_SETUP: InterviewSetup = {
  company: '', title: '', companyUrl: '', jobUrl: '', jobDescription: '', round: 2, persona: 'hiring manager',
  durationMinutes: 30, targetScore: 80, resumeText: '', linkedinText: '', notes: '',
};
export function remainingSeconds(deadline: string | null | undefined, now: number): number | null {
  if (!deadline) return null;
  const value = Date.parse(deadline);
  return Number.isFinite(value) ? Math.max(0, Math.ceil((value - now) / 1000)) : null;
}
export function displayEvidence(value: unknown): string {
  if (value == null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

export function coverageLabel(value: unknown): string {
  if (value && typeof value === "object" && "label" in value && typeof value.label === "string") return value.label;
  return displayEvidence(value);
}

export function setupFromSession(previous: InterviewSession): InterviewSetup {
  return {
    company: previous.company, title: previous.title, companyUrl: previous.companyUrl || '', jobUrl: previous.jobUrl || '',
    jobDescription: previous.jobDescription || '', round: previous.round, persona: previous.persona,
    durationMinutes: previous.durationMinutes, targetScore: previous.targetScore,
    resumeText: previous.resumeText || '', linkedinText: previous.linkedinText || '', notes: previous.notes || '',
    previousSessionId: previous.id,
  };
}
export function setupPayload(setup: InterviewSetup, previous: InterviewSession | null): Partial<InterviewSetup> {
  const payload: Partial<InterviewSetup> = { ...setup };
  if (setup.previousSessionId && previous?.id === setup.previousSessionId) {
    for (const key of ['jobDescription', 'resumeText', 'notes', 'linkedinText'] as const) {
      if (setup[key] === (previous[key] || '')) delete payload[key];
    }
  }
  return payload;
}
export function canRetryJudgment(turn: InterviewTurn, now: number): boolean {
  return turn.scoreStatus === 'failed' || (turn.scoreStatus === 'pending' && !!turn.judgingAt && Date.parse(turn.judgingAt) < now - 5 * 60_000);
}

export function voiceWorkerStatus(transport: string, joined: boolean, audioReceived: boolean, connectedAt: number | null, now: number): { label: string; delayed: boolean } {
  if (transport !== 'Connected') return { label: transport, delayed: false };
  if (audioReceived) return { label: 'Room connected · interviewer audio connected', delayed: false };
  if (joined) return { label: 'Room connected · interviewer joined; waiting for audio', delayed: false };
  const delayed = connectedAt !== null && now - connectedAt >= 20_000;
  return { label: delayed ? 'Voice worker not joined. Start worker then reconnect.' : 'Room connected · waiting for interviewer to join…', delayed };
}

export function canStartInterview(session: InterviewSession): boolean {
  return session.status === 'draft' && (!session.sourceIngestion || session.sourceIngestion.status === 'ready');
}
