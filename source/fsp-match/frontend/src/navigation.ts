export type AccountRole = 'candidate' | 'employer';

const commonPages = ['home', 'invitations', 'vacancies', 'responses', 'microtasks', 'history', 'settings', 'notifications'];
const rolePages: Record<AccountRole, readonly string[]> = {
  candidate: [...commonPages, 'profile', 'assessment'],
  employer: [...commonPages, 'company', 'search', 'shortlist'],
};

/** Resolve before rendering: a stale hash must never mount another role's form. */
export function resolvePage(requested: string, role: AccountRole): string {
  return rolePages[role].includes(requested) ? requested : 'home';
}

export function blockedPageNotice(requested: string, role: AccountRole): string {
  const candidatePage = ['profile', 'assessment'].includes(requested);
  const employerPage = ['company', 'search', 'shortlist'].includes(requested);
  if (resolvePage(requested, role) === requested || (!candidatePage && !employerPage)) return '';
  return role === 'employer'
    ? 'Сейчас открыт аккаунт работодателя. Профиль специалиста и тестирование доступны в аккаунте кандидата. Для прохождения теста выйдите и войдите как кандидат.'
    : 'Сейчас открыт аккаунт кандидата. Компания и подбор специалистов доступны в аккаунте работодателя. Для работы с компанией выйдите и войдите как работодатель.';
}

export function sessionIdentity(user: { id: string; role: AccountRole } | null): string {
  return user ? `${user.id}:${user.role}` : '';
}

export const sessionChangeStorageKey = 'codematch-session-change';
