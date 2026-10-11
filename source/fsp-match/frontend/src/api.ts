export type Row = Record<string, any>;
export interface User { id: string; email: string; role: 'candidate' | 'employer'; email_verified: boolean; is_demo: boolean }
export interface MatchingExplanation {
  source: 'llm_grounded' | 'deterministic_fallback';
  provenance: 'llm_fact_selection' | 'trusted_matching_rules';
  explanation: string[];
  selected_fact_ids: string[];
  facts: { id: string; text: string }[];
  warnings: string[];
  formula_version: string;
  score: number;
  changes_matching: false;
  cached: boolean;
}
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
let csrf = '';
const errorText = (detail: unknown): string => {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((v: Row) => `${(v.loc ?? []).filter((x: string) => x !== 'body').join('.')}: ${v.msg ?? 'проверьте значение'}`).join('; ');
  if (detail && typeof detail === 'object') { const d = detail as Row; return d.message ?? d.reason ?? JSON.stringify(d); }
  return 'Сервер не выполнил запрос. Попробуйте ещё раз.';
};
export async function request<T = Row>(path: string, method = 'GET', body?: unknown): Promise<T> {
  if (method !== 'GET' && !csrf) await session();
  const multipart = body instanceof FormData;
  let res: Response;
  try { res = await fetch(`/api${path}`, { method, credentials: 'include', headers: { ...(multipart || body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(method !== 'GET' ? { 'X-CSRF-Token': csrf } : {}) }, body: body === undefined ? undefined : multipart ? body : JSON.stringify(body) }); }
  catch { throw new ApiError(0, 'Нет соединения с сервером. Проверьте интернет и повторите запрос.'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, errorText(data.detail ?? data.message));
  if (data.csrf_token) csrf = data.csrf_token;
  else if (path === '/auth/logout') csrf = '';
  return data as T;
}
export const session = () => request<{ user: User | null; csrf_token: string }>('/auth/me');
export const items = (data: unknown): Row[] => Array.isArray(data) ? data : data && typeof data === 'object' ? ((data as Row).items ?? []) : [];
export const specs: Record<string, string> = { backend_python: 'Python · Backend', frontend_ts: 'TypeScript · Frontend', data_sql: 'SQL · Аналитика данных' };
export const grades: Record<string, string> = { junior: 'Junior', middle: 'Middle' };
export const workFormats: Record<string, string> = { remote: 'Удалённо', office: 'В офисе', hybrid: 'Гибрид', any: 'Любой формат' };
export const money = (min?: number | null, max?: number | null) => min != null ? `${Number(min).toLocaleString('ru-RU')}${max != null && max !== min ? ` – ${Number(max).toLocaleString('ru-RU')}` : ''} ₽` : 'Не указана';
export const date = (value?: string | number | null) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('ru-RU', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '–';
