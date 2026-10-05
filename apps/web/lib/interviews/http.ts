export async function interviewRequest<T>(path = '', data?: unknown): Promise<T> {
  const response = await fetch(`/api/imx/interviews${path}`, {
    method: data === undefined ? 'GET' : 'POST', cache: 'no-store',
    headers: data === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  return readResponse<T>(response);
}
async function readResponse<T>(response: Response): Promise<T> {
  const value = await response.json().catch(() => null);
  if (!response.ok) throw new Error(value?.error?.message || 'The interview service did not complete this request. Refresh its status before retrying.');
  return value as T;
}
export async function uploadInterviewDocument<T>(id: string, kind: string, file: File): Promise<T> {
  const maxMb = kind === 'prior_recording' ? 25 : 5;
  if (!file.size || file.size > maxMb * 1024 * 1024) throw new Error(`Choose a nonempty file, ${maxMb} MB or smaller.`);
  const data = new FormData(); data.set('file', file); data.set('kind', kind);
  return readResponse<T>(await fetch(`/api/imx/interviews/${encodeURIComponent(id)}/documents`, { method: 'POST', body: data }));
}
