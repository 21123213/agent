/**
 * 推理 Agent API：会话 + SSE（无日志事件）。
 */
import request from "@/utils/request";

export type ReasonerMode = "react" | "plan_execute";

export interface ReasonerSession {
  session_id: string;
  title?: string;
  status?: string;
  agent_type?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ReasonerMessage {
  id?: number;
  role: string;
  content: string;
  created_at?: string;
}

const token = () => localStorage.getItem("access_token") || "";

export async function listReasonerSessions(): Promise<ReasonerSession[]> {
  return request.get("/api/reasoner/sessions");
}

export async function createReasonerSession(
  title?: string
): Promise<ReasonerSession> {
  return request.post("/api/reasoner/sessions", { title });
}

export async function getReasonerHistory(
  sessionId: string
): Promise<{ session_id: string; messages: ReasonerMessage[] }> {
  return request.get(`/api/reasoner/history/${sessionId}`);
}

export async function deleteReasonerSession(sessionId: string): Promise<void> {
  await request.delete(`/api/reasoner/session/${sessionId}`);
}

export type ReasonerSseHandler = (event: string, data: Record<string, unknown>) => void;

/**
 * POST /api/reasoner/chat/stream — 用 fetch 读 SSE（axios 不便流式）。
 */
export async function streamReasonerChat(
  body: {
    query: string;
    session_id?: string;
    mode: ReasonerMode;
    resume?: { resume_token?: string; answers: string };
  },
  onEvent: ReasonerSseHandler,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch("/api/reasoner/chat/stream", {
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
          const data = JSON.parse(raw) as Record<string, unknown>;
          onEvent(eventName, data);
        } catch {
          /* ignore malformed */
        }
        eventName = "message";
      }
    }
  }
}

export function getStoredReasonerSessionId(userId: string | number): string {
  return localStorage.getItem(`reasoner_session_id_${userId}`) || "";
}

export function saveReasonerSessionId(
  userId: string | number,
  sessionId: string
): void {
  localStorage.setItem(`reasoner_session_id_${userId}`, sessionId);
}

export function clearStoredReasonerSessionId(userId: string | number): void {
  localStorage.removeItem(`reasoner_session_id_${userId}`);
}

export function getStoredReasonerMode(): ReasonerMode {
  const m = localStorage.getItem("reasoner_mode");
  return m === "plan_execute" ? "plan_execute" : "react";
}

export function saveReasonerMode(mode: ReasonerMode): void {
  localStorage.setItem("reasoner_mode", mode);
}
