'use client';

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Room, RoomEvent, Track } from 'livekit-client';
import { AppShell, type Connection } from '@/components/shell/AppShell';
import { interviewRequest, uploadInterviewDocument } from '@/lib/interviews/http';
import { DEFAULT_SETUP, canStartInterview, voiceWorkerStatus, canRetryJudgment, setupFromSession, setupPayload, coverageLabel, displayEvidence, remainingSeconds, type InterviewContext, type InterviewIndex, type InterviewSession, type InterviewSetup } from '@/lib/interviews/types';
import styles from './interviews.module.css';
import { AnswerFeedback } from './AnswerFeedback';

export function InterviewsView() {
  const [index, setIndex] = useState<InterviewIndex>({ readiness: {}, sessions: [] });
  const [context, setContext] = useState<InterviewContext>({ resumeText: '', jobs: [] });
  const [setup, setSetup] = useState<InterviewSetup>({ ...DEFAULT_SETUP });
  const [session, setSession] = useState<InterviewSession | null>(null);
  const [connection, setConnection] = useState<Connection>('checking');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [answer, setAnswer] = useState('');
  const [kind, setKind] = useState('prior_transcript');
  const [sourceText, setSourceText] = useState('');
  const [voice, setVoice] = useState('Disconnected');
  const [agentJoined, setAgentJoined] = useState(false);
  const [remoteAudio, setRemoteAudio] = useState(false);
  const [voiceConnectedAt, setVoiceConnectedAt] = useState<number | null>(null);
  const [mic, setMic] = useState(false);
  const [now, setNow] = useState(Date.now());
  const room = useRef<Room | null>(null);
  const audio = useRef<HTMLDivElement>(null);
  const activeId = useRef<string | null>(null);
  const finishing = useRef(false);
  const repeatSource = useRef<InterviewSession | null>(null);
  const pendingAnswer = useRef<{ answer: string; requestId: string } | null>(null);
  const workerStatus = voiceWorkerStatus(voice, agentJoined, remoteAudio, voiceConnectedAt, now);
  const seconds = remainingSeconds(session?.deadlineAt, now);
  const disconnect = useCallback(() => {
    const current = room.current; room.current = null;
    if (current) { for (const publication of current.localParticipant.audioTrackPublications.values()) publication.track?.stop(); void current.disconnect(); }
    audio.current?.replaceChildren(); setMic(false); setVoice('Disconnected'); setAgentJoined(false); setRemoteAudio(false); setVoiceConnectedAt(null);
  }, []);
  const refresh = useCallback(async () => {
    const result = await interviewRequest<InterviewIndex>(); setIndex(result); setConnection('connected');
  }, []);
  useEffect(() => {
    void refresh().catch((e: Error) => { setError(e.message); setConnection('unavailable'); });
    void interviewRequest<InterviewContext>('/context').then((value) => { setContext(value); setSetup((old) => ({ ...old, resumeText: old.resumeText || value.resumeText || '' })); }).catch(() => setContext({ resumeText: '', jobs: [], warnings: ['Existing candidate and tracked-job evidence could not be loaded. You can enter the practice context manually.'] }));
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => { clearInterval(timer); disconnect(); };
  }, [refresh, disconnect]);
  useEffect(() => {
    const id = session?.id;
    if (!id) return;
    const poll = setInterval(() => {
      void interviewRequest<InterviewSession>(`/${encodeURIComponent(id)}`).then((value) => {
        if (activeId.current === id) { setSession(value); setIndex((old) => ({ ...old, sessions: old.sessions.map((item) => item.id === value.id ? value : item) })); setConnection('connected'); }
      }).catch((e: Error) => { if (activeId.current === id) { setError(e.message); setConnection('unavailable'); } });
    }, 2500);
    return () => clearInterval(poll);
  }, [session?.id]);
  const finish = useCallback(async () => {
    if (!activeId.current || finishing.current) return;
    finishing.current = true; disconnect(); setBusy(true); setError('');
    try { const result = await interviewRequest<InterviewSession>(`/${encodeURIComponent(activeId.current)}/finish`, {}); setSession(result); await refresh(); }
    catch (e) { setError((e as Error).message); }
    finally { finishing.current = false; setBusy(false); }
  }, [disconnect, refresh]);
  useEffect(() => {
    if (session?.status === 'completed') disconnect();
    if (session?.status === 'active' && seconds === 0) { disconnect(); void finish(); }
  }, [session?.status, seconds, finish, disconnect]);
  async function action(operation: () => Promise<void>) { setBusy(true); setError(''); try { await operation(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  function select(value: InterviewSession | null) { disconnect(); activeId.current = value?.id || null; pendingAnswer.current = null; setAnswer(''); setSession(value); setError(''); }
  async function create(event: FormEvent) { event.preventDefault(); await action(async () => { const payload = setupPayload(setup, repeatSource.current);
      const value = await interviewRequest<InterviewSession>('', payload); select(value); await refresh(); }); }
  function revise(previous: InterviewSession) { repeatSource.current = previous; select(null); setSetup(setupFromSession(previous)); }
  const isRevision = !!setup.previousSessionId && repeatSource.current?.status === 'draft';
  async function start() { await action(async () => { const value = await interviewRequest<InterviewSession>(`/${session!.id}/start`, {}); setSession(value); await refresh(); }); }
  async function connectVoice() {
    await action(async () => {
      disconnect(); setVoice('Connecting…');
      const liveRoom = new Room(); room.current = liveRoom;
      const syncParticipants = () => {
        if (room.current !== liveRoom) return;
        const participants = [...liveRoom.remoteParticipants.values()];
        setAgentJoined(participants.some((participant) => participant.isAgent));
        setRemoteAudio(participants.some((participant) => [...participant.audioTrackPublications.values()].some((publication) => publication.isSubscribed)));
      };
      liveRoom.on(RoomEvent.ParticipantConnected, syncParticipants);
      liveRoom.on(RoomEvent.ParticipantDisconnected, () => { syncParticipants(); setVoiceConnectedAt(Date.now()); });
      liveRoom.on(RoomEvent.TrackSubscribed, (track) => { if (room.current === liveRoom && track.kind === Track.Kind.Audio) { const element = track.attach(); element.autoplay = true; audio.current?.append(element); setRemoteAudio(true); syncParticipants(); } });
      liveRoom.on(RoomEvent.TrackUnsubscribed, (track) => { track.detach().forEach((element) => element.remove()); syncParticipants(); });
      liveRoom.on(RoomEvent.Reconnecting, () => { if (room.current === liveRoom) setVoice('Reconnecting…'); });
      liveRoom.on(RoomEvent.Reconnected, () => { if (room.current === liveRoom) { setVoice('Connected'); setVoiceConnectedAt(Date.now()); syncParticipants(); } });
      liveRoom.on(RoomEvent.Disconnected, () => { if (room.current === liveRoom) { room.current = null; setVoice('Disconnected · reconnect to continue'); setAgentJoined(false); setRemoteAudio(false); setVoiceConnectedAt(null); setMic(false); audio.current?.replaceChildren(); } });
      liveRoom.on(RoomEvent.MediaDevicesError, () => setError('Microphone unavailable. Check browser permission and your selected audio device, then reconnect.'));
      try {
        const credentials = await interviewRequest<{url: string; token: string; roomName: string}>(`/${session!.id}/connect`, {});
        if (room.current !== liveRoom) return;
        await liveRoom.connect(credentials.url, credentials.token);
        if (room.current !== liveRoom) { await liveRoom.disconnect(); return; }
        await liveRoom.startAudio();
        await liveRoom.localParticipant.setMicrophoneEnabled(true);
        if (room.current !== liveRoom) { await liveRoom.disconnect(); return; }
        setMic(true); setVoice('Connected'); setVoiceConnectedAt(Date.now()); syncParticipants();
      } catch (e) { disconnect(); throw new Error(`Voice could not connect: ${(e as Error).message}`); }
    });
  }
  async function submitAnswer(event: FormEvent) {
    event.preventDefault();
    await action(async () => {
      if (!pendingAnswer.current || pendingAnswer.current.answer !== answer) pendingAnswer.current = { answer, requestId: crypto.randomUUID() };
      const value = await interviewRequest<InterviewSession>(`/${session!.id}/turns`, pendingAnswer.current);
      setSession(value); setAnswer(''); pendingAnswer.current = null;
    });
  }
  const set = <K extends keyof InterviewSetup>(key: K, value: InterviewSetup[K]) => setSetup((old) => ({ ...old, [key]: value }));
  const readiness = Object.entries(index.readiness);
  return <AppShell mode="live" section="interviews" connection={connection} colophon="Practice with evidence. Readiness is an internal benchmark, never an offer probability." skipLabel="Skip to interview practice">
    <div className={styles.page}>
      <header className={styles.hero}><div><p className={styles.eyebrow}>THE PRACTICE ROOM</p><h1>Make your next answer<br /><span>the stronger one.</span></h1><p>Real job context. A demanding interviewer. Evidence you can improve.</p></div><button className={styles.primary} disabled={busy || session?.status === 'active'} onClick={() => { select(null); setSetup({ ...DEFAULT_SETUP, resumeText: context.resumeText || '' }); }}>New interview <span aria-hidden="true">↗</span></button></header>
      {error && <div className={styles.error} role="alert">{error}<button onClick={() => void action(refresh)}>Refresh status</button></div>}
      <details className={styles.readiness}><summary>Provider readiness · {connection === 'connected' ? 'service connected' : connection}</summary>{readiness.length ? readiness.map(([name, value]) => <p key={name}><strong>{name}</strong> {displayEvidence(value)}</p>) : <p>Provider readiness has not been verified.</p>}</details>
      <div className={styles.layout}>
        <aside className={styles.history}><p className={styles.eyebrow}>PRACTICE HISTORY</p><h2>Your repetitions</h2>{index.sessions.length === 0 && <p>No practices yet. Start with the role in front of you.</p>}{index.sessions.map((item) => <button key={item.id} className={session?.id === item.id ? styles.selected : ''} disabled={busy || (session?.status === 'active' && item.id !== session.id)} onClick={() => void action(async () => select(await interviewRequest<InterviewSession>(`/${item.id}`)))}><strong>{item.company}</strong><span>{item.title}</span><small>Round {item.round} · {item.status} · {new Date(item.createdAt).toLocaleDateString()}</small><b>{item.report?.overallScore != null ? `${item.report.overallScore}/100` : item.status === 'completed' ? 'No completed grade' : `${item.durationMinutes} min`}</b></button>)}</aside>
        <section className={styles.workspace}>
          {!session ? <form onSubmit={create} className={styles.form}>
            {context.warnings?.map((warning, i) => <p key={i} className={styles.warning}>{warning}</p>)}<div className={styles.sectionTitle}><span>01 / PREPARE</span><h2>Give the interviewer something real.</h2><p>{isRevision ? 'Save a revised draft with your existing sources. The original draft stays in history.' : 'Round two and 30 minutes are editable starting points.'}</p></div>
            {context.jobs.length > 0 && <label>Choose a tracked job<select defaultValue="" onChange={(event) => { const job = context.jobs.find((item) => item.id === event.target.value); if (job) setSetup((old) => ({ ...old, company: job.company, title: job.title, companyUrl: job.companyUrl || '', jobUrl: job.jobUrl || '', jobDescription: job.jobDescription || '', notes: job.notes || '' })); }}><option value="">Enter a job manually</option>{context.jobs.map((job) => <option key={job.id} value={job.id}>{job.company} · {job.title}</option>)}</select></label>}
            <div className={styles.row}><label>Company<input required value={setup.company} onChange={(e) => set('company', e.target.value)} /></label><label>Role title<input required value={setup.title} onChange={(e) => set('title', e.target.value)} /></label></div>
            <label>Company website URL<input type="url" required value={setup.companyUrl} onChange={(e) => set('companyUrl', e.target.value)} placeholder="https://company.com" /></label>
            <label>Job application URL<input type="url" required value={setup.jobUrl} onChange={(e) => set('jobUrl', e.target.value)} placeholder="https://jobs.ashbyhq.com/…" /></label>
            <p className={styles.hint}>We will read both websites and index the company and job context. You do not need to paste a job description.</p>
            <div className={styles.row}><label>Interview round<input type="number" min={1} max={10} value={setup.round} onChange={(e) => { const round = Number(e.target.value); setSetup((old) => ({ ...old, round, persona: round === 1 ? 'recruiter' : round === 2 ? 'hiring manager' : 'executive' })); }} /></label><label>Interviewer role<input required value={setup.persona} onChange={(e) => set('persona', e.target.value)} /></label><label>Duration (minutes)<input type="number" min={1} max={180} value={setup.durationMinutes} onChange={(e) => set('durationMinutes', Number(e.target.value))} /></label><label>Readiness target<input type="number" min={0} max={100} value={setup.targetScore} onChange={(e) => set('targetScore', Number(e.target.value))} /></label></div>
            <label>Resume / candidate evidence<textarea rows={5} value={setup.resumeText} onChange={(e) => set('resumeText', e.target.value)} placeholder="Paste your resume or attach a document after saving." /></label><p className={styles.hint}>{context.resumeText ? 'Existing candidate evidence is reused. Review and edit it for this practice.' : 'No existing resume text was available. Add your evidence; missing facts will not be invented.'}</p>
            <label>Company and interview notes<textarea rows={3} value={setup.notes} onChange={(e) => set('notes', e.target.value)} /></label><label>LinkedIn profile text (optional)<textarea rows={3} value={setup.linkedinText} onChange={(e) => set('linkedinText', e.target.value)} /></label>
            <p className={styles.hint}>You can add prior-round transcripts, recordings and resume files after saving.</p><button className={styles.primary} disabled={busy}>{busy ? 'Saving…' : isRevision ? 'Save revised setup' : 'Save practice setup'}</button>
          </form> : <>
            <div className={styles.sectionTitle}><span>ROUND {session.round} / {session.status.toUpperCase()}</span><h2>{session.title}</h2><p>{session.company} · {session.persona} · {session.durationMinutes} minutes · target {session.targetScore}/100</p></div>
            {session.contextWarnings?.map((warning, i) => <p className={styles.warning} key={i}>{warning}</p>)}
            {session.status === 'draft' && <>
              <button disabled={busy} onClick={() => revise(session)}>Edit setup</button>
              {session.sourceIngestion?.status === 'processing' && <p className={styles.warning} role="status">Reading the company website and job application page… indexing their context before practice can start.</p>}
              {session.sourceIngestion?.status === 'ready' && <p className={styles.hint} role="status">Website context is ready. Review the saved context and sources before starting.</p>}
              {session.sourceIngestion?.status === 'failed' && <div className={styles.error} role="alert">Website import failed. {session.sourceIngestion.error || 'Check the source URLs and try again.'}<button disabled={busy} onClick={() => void action(async () => setSession(await interviewRequest<InterviewSession>(`/${session.id}/ingest`, {})))}>Retry website import</button></div>}
              {!session.sourceIngestion && <p className={styles.warning}>This saved practice uses its existing context. Edit setup to add website sources.</p>}
              <details className={styles.context}><summary>Review saved job and candidate context</summary><h3>Job description</h3><p>{session.jobDescription || 'The job description will appear after website import.'}</p><h3>Candidate evidence</h3><p>{session.resumeText || 'No resume text supplied.'}</p><h3>Notes</h3><p>{session.notes || 'No notes supplied.'}</p>{session.companyUrl && <p><a href={session.companyUrl} target="_blank" rel="noreferrer">Company website ↗</a></p>}{session.jobUrl && <a href={session.jobUrl} target="_blank" rel="noreferrer">Job source ↗</a>}</details>
              <section className={styles.sources}><h3>Bring the previous conversation.</h3><p>Recordings are transcribed with ElevenLabs, then indexed with source identity. Text and resume documents are indexed for retrieval.</p><label>Source type<select value={kind} onChange={(e) => setKind(e.target.value)}><option value="prior_transcript">Previous-round transcript</option><option value="prior_recording">Previous-round recording</option><option value="resume">Resume</option><option value="notes">Notes</option><option value="linkedin">LinkedIn text</option></select></label><label>Upload context file (recording 25 MB; document 5 MB)<input type="file" disabled={busy} accept={kind === 'prior_recording' ? 'audio/*,video/mp4' : '.txt,.md,.vtt,.srt,.pdf,.docx'} onChange={(event) => { const file = event.target.files?.[0]; if (file) void action(async () => { setSession(await uploadInterviewDocument<InterviewSession>(session.id, kind, file)); }); event.target.value = ''; }} /></label>{kind !== 'prior_recording' && <><label>Or paste source text<textarea value={sourceText} onChange={(e) => setSourceText(e.target.value)} rows={4} /></label><button disabled={busy || !sourceText.trim()} onClick={() => void action(async () => { setSession(await interviewRequest<InterviewSession>(`/${session.id}/documents`, { kind, name: kind === 'prior_transcript' ? 'Previous-round transcript' : kind, text: sourceText })); setSourceText(''); })}>Index pasted source</button></>}{busy && <p role="status">Processing request… recordings may take a few minutes.</p>}</section>
              <button className={styles.primary} disabled={busy || !canStartInterview(session)} onClick={() => void start()}>Start {session.durationMinutes}-minute practice</button><p className={styles.hint}>Starts the server timer. You enable the microphone in the next step.</p>
            </>}
            {!!session.documents?.length && <div className={styles.documents}><h3>Context sources</h3>{session.documents.map((doc) => <p key={doc.id}><strong>{doc.sourceUrl ? <a href={doc.sourceUrl} target="_blank" rel="noreferrer">{doc.name} ↗</a> : doc.name}</strong><span>{doc.status} · {doc.chunkCount || 0} chunks</span>{doc.error && <span role="alert">{doc.error} · {doc.sourceUrl ? 'Retry website import to try again.' : 'Re-upload this source to try again.'}</span>}</p>)}</div>}
            {session.status === 'active' && <section className={styles.live}>
              <div className={styles.liveHeader}><span>LIVE PRACTICE</span><strong aria-label="Time remaining">{seconds == null ? 'Timer syncing' : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`}</strong></div>
              <>{session.questionError && <div className={styles.error} role="alert">{session.questionError}<button disabled={busy} onClick={() => void start()}>Retry interviewer</button></div>}</><h3>{session.currentQuestion || 'Your interviewer is preparing the opening question.'}</h3><p role="status">{workerStatus.label} · Microphone {mic ? 'on' : 'off'}</p>{workerStatus.delayed && <p className={styles.warning}>The room is connected, but no interviewer has joined. Start the interview voice worker, then reconnect. Your practice timer keeps running.</p>}<div className={styles.actions}>{!room.current ? <button className={styles.primary} disabled={busy || seconds === 0} onClick={() => void connectVoice()}>Enable microphone & connect</button> : <button disabled={busy} onClick={() => void action(async () => { await room.current?.localParticipant.setMicrophoneEnabled(!mic); setMic(!mic); })}>{mic ? 'Mute microphone' : 'Unmute microphone'}</button>}{room.current && <button disabled={busy || seconds === 0} onClick={() => void connectVoice()}>Reconnect voice</button>}<button disabled={finishing.current} onClick={() => void finish()}>End practice</button></div><p className={styles.hint}>Speak naturally and interrupt when needed. Your microphone stays off until you enable it.</p>
              <details><summary>Use text fallback</summary><form onSubmit={submitAnswer}><label>Your answer<textarea required rows={4} value={answer} onChange={(e) => setAnswer(e.target.value)} /></label><button disabled={busy || !answer.trim()}>Send answer</button></form></details>
            </section>}
            {session.report && <section className={styles.report}><p className={styles.eyebrow}>JEV / PRACTICE SCORECARD</p><div className={styles.score}>{session.report.overallScore == null ? 'Pending' : session.report.overallScore}<small>{session.report.overallScore != null && '/100'}</small></div><p>{session.report.gradedAnswers} of {session.report.totalAnswers} answers graded · {session.report.rubricVersion || 'Rubric pending'}</p>{session.report.scoreChange != null && <p>{session.report.scoreChange > 0 ? '+' : ''}{session.report.scoreChange} points from the previous comparable practice.</p>}<p className={styles.hint}>{coverageLabel(session.report.coverage)}</p><div className={styles.dimensions}>{Object.entries(session.report.dimensions || {}).map(([name, score]) => <div key={name}><span>{name}</span><strong>{score}</strong><meter min={0} max={100} value={score} aria-label={name} /></div>)}</div><h3>Work on next</h3>{session.report.drillProvenance && <p className={styles.hint}>{session.report.drillProvenance}</p>}{session.report.nextDrills?.length ? <ul>{session.report.nextDrills.map((drill, i) => <li key={i}>{drill}</li>)}</ul> : <p>No grounded drills available yet. Pending or failed judgments are not scores.</p>}{!!session.report.unansweredIssues?.length && <><h3>Unanswered issues</h3><ul>{session.report.unansweredIssues.map((issue, i) => <li key={i}>{issue}</li>)}</ul></>}</section>}
            {session.status === 'completed' && <button className={styles.primary} disabled={busy} onClick={() => revise(session)}>Practice again with this context ↗</button>}
            <section className={styles.transcript}><h3>Conversation & judgments</h3>{!session.turns?.length && <p>Completed answers will appear here. Jev grades asynchronously while the conversation continues.</p>}{session.turns?.map((turn, i) => <article key={turn.id}><p className={styles.eyebrow}>ANSWER {i + 1}</p><h4>{turn.question}</h4><p className={styles.answer}>{turn.answer}</p><div className={styles.grade}>{turn.scoreStatus === 'completed' ? `Jev · ${turn.score?.overallScore ?? 'Unavailable'}/100` : turn.scoreStatus === 'failed' ? 'Judgment failed · no score' : canRetryJudgment(turn, now) ? 'Judgment delayed; retry available' : 'Jev judgment pending'}</div>{turn.error && <p className={styles.error}>{turn.error}</p>}{canRetryJudgment(turn, now) && <button disabled={busy} onClick={() => void action(async () => setSession(await interviewRequest<InterviewSession>(`/${session.id}/turns`, { answer: turn.answer, requestId: turn.requestId, question: turn.question })))}>Retry judgment</button>}{turn.score && <AnswerFeedback score={turn.score} />}{turn.score && <details><summary>Grading evidence</summary><pre>{displayEvidence(turn.score)}</pre></details>}{turn.retrieval != null && <details><summary>Retrieved source evidence</summary><pre>{displayEvidence(turn.retrieval)}</pre></details>}</article>)}</section>
          </>}
        </section>
      </div><div ref={audio} hidden />
    </div>
  </AppShell>;
}
