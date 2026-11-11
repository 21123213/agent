/**
 * 智能问数 API：会话 + SSE。
 */
import request from "@/utils/request";

export interface AskdataSession {
  session_id: string;
  title?: string;
  status?: string;
  agent_type?: string;
  created_at?: string;
  updated_at?: string;
}

export interface AskdataMessage {
  id?: number;
  role: string;
  content: string;
  created_at?: string;
}

const token = () => localStorage.getItem("access_token") || "";

const SESSION_KEY = (userId: string | number) =>
  `askdata_session_id_${userId}`;

export function getStoredAskdataSessionId(userId: string | number): string {
  return localStorage.getItem(SESSION_KEY(userId)) || "";
}

export function saveAskdataSessionId(
  userId: string | number,
  sessionId: string
) {
  localStorage.setItem(SESSION_KEY(userId), sessionId);
}

export function clearStoredAskdataSessionId(userId: string | number) {
  localStorage.removeItem(SESSION_KEY(userId));
}

export async function listAskdataSessions(): Promise<AskdataSession[]> {
  return request.get("/api/askdata/sessions");
}

export async function createAskdataSession(
  title?: string
): Promise<AskdataSession> {
  return request.post("/api/askdata/sessions", { title });
}

export async function getAskdataHistory(
  sessionId: string
): Promise<{ session_id: string; messages: AskdataMessage[] }> {
  return request.get(`/api/askdata/history/${sessionId}`);
}

export async function deleteAskdataSession(sessionId: string): Promise<void> {
  await request.delete(`/api/askdata/session/${sessionId}`);
}

export type AskdataSseHandler = (
  event: string,
  data: Record<string, unknown>
) => void;

/** POST /api/askdata/chat/stream */
export async function streamAskdataChat(
  body: { query: string; session_id?: string },
  onEvent: AskdataSseHandler,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch("/api/askdata/chat/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token()}`,
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let eventName = "message";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n");
    buf = parts.pop() || "";
    for (const line of parts) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        const raw = line.slice(5).trim();
        try {
          onEvent(eventName, JSON.parse(raw) as Record<string, unknown>);
        } catch {
          onEvent(eventName, { raw });
        }
        eventName = "message";
      }
    }
  }
}
