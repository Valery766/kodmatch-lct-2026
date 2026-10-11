type Draft = Record<string, unknown>;
type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

const keyFor = (userId: string, attemptId: string) => `codematch-assessment:${userId}:${attemptId}`;

export function readAssessmentDraft(storage: DraftStorage, userId: string, attemptId: string, expiresAt: number, now = Date.now()): Draft {
  try {
    const key = keyFor(userId, attemptId);
    const raw = storage.getItem(key);
    if (!raw) return {};
    const saved = JSON.parse(raw);
    if (expiresAt <= now || saved.expiresAt !== expiresAt || !saved.answers || typeof saved.answers !== 'object' || Array.isArray(saved.answers)) {
      storage.removeItem(key);
      return {};
    }
    return Object.fromEntries(Object.entries(saved.answers).filter(([, value]) => typeof value === 'string' || (Array.isArray(value) && value.every(item => typeof item === 'string'))));
  } catch {
    return {};
  }
}

export function writeAssessmentDraft(storage: DraftStorage, userId: string, attemptId: string, expiresAt: number, answers: Draft): void {
  try { storage.setItem(keyFor(userId, attemptId), JSON.stringify({ expiresAt, answers })); } catch { /* Private mode/storage limits must not block the test. */ }
}

export function removeAssessmentDraft(storage: DraftStorage, userId: string, attemptId: string): void {
  try { storage.removeItem(keyFor(userId, attemptId)); } catch { /* Test submission does not depend on local storage. */ }
}
