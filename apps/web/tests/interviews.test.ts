import { describe, expect, it, vi } from 'vitest';
import { proxy, gatewayConfigFromEnv } from '@/lib/gateway';
import { DEFAULT_SETUP, canStartInterview, voiceWorkerStatus, canRetryJudgment, setupFromSession, setupPayload, remainingSeconds, type InterviewSession, type InterviewTurn } from '@/lib/interviews/types';
const origin = 'http://127.0.0.1:4317';
const config = gatewayConfigFromEnv({ IMX_BACKEND_URL: 'http://127.0.0.1:8765', IMX_WEB_ORIGIN: origin });
describe('interview gateway', () => {
  it('translates recording uploads with encoded source identity and kind', async () => {
    const form = new FormData(); form.set('kind', 'prior_recording'); form.set('file', new File(['audio fixture'], 'round 1.wav'));
    const upstream = vi.fn(async (_url: URL | RequestInfo, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      expect(headers.get('X-Imx-Filename')).toBe('round%201.wav');
      expect(headers.get('X-Imx-Kind')).toBe('prior_recording');
      expect(headers.get('origin')).toBe(origin);
      expect(headers.get('Content-Type')).toBe('application/octet-stream');
      return Response.json({ id: 'fixture' });
    });
    const response = await proxy(new Request(`${origin}/api/imx/interviews/fixture/documents`, { method: 'POST', headers: { Origin: origin }, body: form }), ['interviews', 'fixture', 'documents'], config, upstream as typeof fetch);
    expect(response.status).toBe(200); expect(upstream).toHaveBeenCalledOnce();
  });
  it('rejects cross-origin uploads before forwarding', async () => {
    const upstream = vi.fn();
    const response = await proxy(new Request(`${origin}/api/imx/interviews/fixture/documents`, { method: 'POST', headers: { Origin: 'https://attacker.test' }, body: 'x' }), ['interviews', 'fixture', 'documents'], config, upstream);
    expect(response.status).toBe(403); expect(upstream).not.toHaveBeenCalled();
  });
  it('rejects declared oversized recording before parsing or forwarding', async () => {
    const upstream = vi.fn();
    const response = await proxy(new Request(`${origin}/api/imx/interviews/f/documents`, { method: 'POST', headers: { Origin: origin, 'Content-Type': 'multipart/form-data', 'Content-Length': String(26 * 1024 * 1024) }, body: 'x' }), ['interviews', 'f', 'documents'], config, upstream);
    expect(response.status).toBe(413); expect(upstream).not.toHaveBeenCalled();
  });
});
describe('server deadline presentation', () => {
  it('rounds up and never goes negative', () => {
    expect(remainingSeconds('2026-10-02T12:00:00Z', Date.parse('2026-10-02T11:59:58.100Z'))).toBe(2);
    expect(remainingSeconds('2026-10-02T12:00:00Z', Date.parse('2026-10-02T12:01:00Z'))).toBe(0);
    expect(remainingSeconds(null, Date.now())).toBeNull();
  });
});

describe('revising a saved draft', () => {
  it('changes duration and persona while preserving uploaded evidence through previousSessionId', () => {
    const previous: InterviewSession = { ...DEFAULT_SETUP, id: 'draft-1', company: 'Fixture', title: 'Lead', status: 'draft', createdAt: '2026-10-02T12:00:00Z', documents: [{id: 'pdf', kind: 'resume', name: 'resume.pdf', status: 'ready', chunkCount: 2}], turns: [], contextWarnings: [] };
    const setup = setupFromSession(previous);
    setup.durationMinutes = 45; setup.persona = 'executive';
    const payload = setupPayload(setup, previous);
    expect(payload.previousSessionId).toBe('draft-1');
    expect(payload.durationMinutes).toBe(45); expect(payload.persona).toBe('executive');
    expect(payload.resumeText).toBeUndefined(); expect(payload.notes).toBeUndefined();
    setup.notes = 'New evidence';
    expect(setupPayload(setup, previous).notes).toBe('New evidence');
    expect(previous.durationMinutes).toBe(30);
  });
});
describe('judgment recovery', () => {
  const turn: InterviewTurn = {id: 't1', requestId: 'r1', question: 'Q', answer: 'A', scoreStatus: 'pending', judgingAt: '2026-10-02T12:00:00Z'};
  it('permits manual retry only after the backend five minute stale threshold', () => {
    expect(canRetryJudgment(turn, Date.parse('2026-10-02T12:05:00Z'))).toBe(false);
    expect(canRetryJudgment(turn, Date.parse('2026-10-02T12:05:01Z'))).toBe(true);
    expect(canRetryJudgment({...turn, scoreStatus: 'completed'}, Date.parse('2026-10-02T12:06:00Z'))).toBe(false);
    expect(canRetryJudgment({...turn, scoreStatus: 'failed'}, Date.parse('2026-10-02T12:00:01Z'))).toBe(true);
  });
});

describe('voice worker availability', () => {
  it('separates room transport from interviewer presence and exposes a delayed worker', () => {
    expect(voiceWorkerStatus('Connected', false, false, 1000, 1001).label).toContain('waiting for interviewer');
    expect(voiceWorkerStatus('Connected', false, false, 1000, 20_999).delayed).toBe(false);
    expect(voiceWorkerStatus('Connected', false, false, 1000, 21_000)).toEqual({label: 'Voice worker not joined. Start worker then reconnect.', delayed: true});
    expect(voiceWorkerStatus('Connected', true, false, 1000, 21_000).label).toContain('interviewer joined; waiting for audio');
    expect(voiceWorkerStatus('Connected', false, true, 1000, 21_000).label).toContain('interviewer audio connected');
    expect(voiceWorkerStatus('Disconnected', false, false, 1000, 21_000).delayed).toBe(false);
  });
});

describe('website context before practice', () => {
  const session: InterviewSession = {...DEFAULT_SETUP, companyUrl: 'https://company.example.com', jobUrl: 'https://jobs.example.com/role', id: 'web-source', status: 'draft', createdAt: '2026-10-02T12:00:00Z', documents: [], turns: [], contextWarnings: []};
  it('waits for requested imports but preserves legacy practice availability', () => {
    expect(canStartInterview({...session, sourceIngestion: {status: 'processing'}})).toBe(false);
    expect(canStartInterview({...session, sourceIngestion: {status: 'failed', error: 'Provider failed'}})).toBe(false);
    expect(canStartInterview({...session, sourceIngestion: {status: 'ready'}})).toBe(true);
    expect(canStartInterview({...session, companyUrl: '', sourceIngestion: {status: 'ready'}})).toBe(true);
    expect(canStartInterview({...session, companyUrl: '', jobUrl: ''})).toBe(true);
    expect(canStartInterview({...session, status: 'completed'})).toBe(false);
  });
  it('retains company URL on revision and tolerates legacy records without one', () => {
    expect(setupFromSession(session).companyUrl).toBe('https://company.example.com');
    const legacy = {...session}; delete (legacy as Partial<InterviewSession>).companyUrl;
    expect(setupFromSession(legacy).companyUrl).toBe('');
    const setup = setupFromSession(session); setup.companyUrl = 'https://new.example.com';
    expect(setupPayload(setup, session).companyUrl).toBe('https://new.example.com');
  });
});
