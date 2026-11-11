export type UserInfo = {
  id: number;
  username: string;
  phone?: string | null;
  unit_id?: number | null;
  role_id?: number | null;
  is_active?: boolean;
};

const TOKEN_KEY = "access_token";
const USER_KEY = "user_info";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function getUserInfo(): UserInfo | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as UserInfo;
  } catch {
    return null;
  }
}

export function setUserInfo(user: UserInfo) {
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function isAuthenticated(): boolean {
  return Boolean(getToken());
}

export function logout() {
  clearAuth();
}
