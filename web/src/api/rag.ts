/**
 * RAG 问答 API 客户端（对齐 /api/rag）。
 *
 * - 会话 CRUD 走 axios（request）
 * - 流式问答用 fetch + ReadableStream（需带 Authorization，axios 不便 SSE）
 *
 * localStorage 键：rag_session_id_{userId}_{kbId}（换 KB 换会话）
 */
import request from "@/utils/request";
import { getUserInfo } from "@/utils/auth";

const CHAT_TIMEOUT_MS = 180_000;
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

export type RagSource = {
  content: string;
  score: number;
  metadata?: Record<string, unknown>;
};

export type RagSession = {
  session_id: string;
  knowledge_base_id?: number | null;
  title?: string | null;
  status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  welcome_hint?: string | null;
};

export type RagMessage = {
  id?: number;
  role: "user" | "assistant" | "system" | string;
  content: string;
  sources?: RagSource[] | null;
  created_at?: string | null;
};

export type RagStreamPhase =
  | "preparing"
  | "thinking"
  | "planning"
  | "retrieving"
  | "acting"
  | "reviewing"
  | "generating"
  | "waiting_user"
  | string;

/** quick=快速回答(baseline)；deep=深度思考(full) */
export type RagAnswerMode = "quick" | "deep";

export type RagChatStreamBody = {
  query: string;
  knowledge_base_id: number;
  session_id?: string | null;
  file_ids?: number[] | null;
  vector_weight?: number;
  bm25_weight?: number;
  answer_mode?: RagAnswerMode;
};

const ANSWER_MODE_KEY = "rag_answer_mode";

export function getStoredRagAnswerMode(): RagAnswerMode {
  const m = localStorage.getItem(ANSWER_MODE_KEY);
  return m === "quick" ? "quick" : "deep";
}

export function saveRagAnswerMode(mode: RagAnswerMode): void {
  localStorage.setItem(ANSWER_MODE_KEY, mode);
}

export type RagStreamCallbacks = {
  onMeta?: (data: {
    session_id: string;
    knowledge_base_id?: number;
    cache_hit?: boolean;
    request_id?: string;
  }) => void;
  onStatus?: (data: {
    phase: RagStreamPhase;
    cache_hit?: boolean;
    node?: string;
    tools?: string[];
    detail?: string;
  }) => void;
  /** 中间过程文字（思考摘要 / 检索预览），非终答 */
  onProgress?: (data: { text: string; node?: string }) => void;
  onDelta?: (data: { content: string }) => void;
  onDone?: (data: {
    session_id: string;
    answer: string;
    sources: RagSource[];
    cache_hit?: boolean;
    knowledge_base_id?: number;
  }) => void;
  onError?: (message: string) => void;
};

// ---------------------------------------------------------------------------
// session localStorage：按 user + kb 隔离
// ---------------------------------------------------------------------------

export function ragSessionStorageKey(kbId: number): string {
  const user = getUserInfo();
  const uid = user?.id != null ? String(user.id) : "anon";
  return `rag_session_id_${uid}_${kbId}`;
}

export function getStoredRagSessionId(kbId: number): string | null {
  return localStorage.getItem(ragSessionStorageKey(kbId));
}

export function saveRagSessionId(kbId: number, sessionId: string): void {
  localStorage.setItem(ragSessionStorageKey(kbId), sessionId);
}

export function clearStoredRagSessionId(kbId: number): void {
  localStorage.removeItem(ragSessionStorageKey(kbId));
}

// ---------------------------------------------------------------------------
// REST
// ---------------------------------------------------------------------------

export const listRagSessions = (knowledgeBaseId: number) => {
  return request.get<any, RagSession[]>("/api/rag/sessions", {
    params: { knowledge_base_id: knowledgeBaseId },
  });
};

export const createRagSession = (body: {
  knowledge_base_id: number;
  title?: string;
}) => {
  return request.post<any, RagSession>("/api/rag/sessions", body);
};

export const getRagHistory = (sessionId: string) => {
  return request.get<any, { session_id: string; messages: RagMessage[] }>(
    `/api/rag/history/${sessionId}`,
  );
};

export const deleteRagSession = (sessionId: string) => {
  return request.delete<any, { success: boolean; message: string }>(
    `/api/rag/session/${sessionId}`,
  );
};

export const clearRagSessions = (knowledgeBaseId: number) => {
  return request.delete<any, { success: boolean; message: string }>(
    "/api/rag/clear",
    { params: { knowledge_base_id: knowledgeBaseId } },
  );
};

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------

type SSEPayload = { event: string; data: Record<string, unknown> };

const parseSSEBlock = (block: string): SSEPayload | null => {
  const lines = block.split("\n");
  let event = "message";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
};

const dispatchSSE = (payload: SSEPayload, callbacks: RagStreamCallbacks) => {
  const { event, data } = payload;
  switch (event) {
    case "meta":
      callbacks.onMeta?.({
        session_id: String(data.session_id || ""),
        knowledge_base_id:
          data.knowledge_base_id != null
            ? Number(data.knowledge_base_id)
            : undefined,
        cache_hit: Boolean(data.cache_hit),
        request_id: data.request_id != null ? String(data.request_id) : undefined,
      });
      break;
    case "status":
      callbacks.onStatus?.({
        phase: (data.phase as RagStreamPhase) || "retrieving",
        cache_hit: data.cache_hit != null ? Boolean(data.cache_hit) : undefined,
        node: data.node != null ? String(data.node) : undefined,
        tools: Array.isArray(data.tools)
          ? data.tools.map((t) => String(t))
          : undefined,
        detail: data.detail != null ? String(data.detail) : undefined,
      });
      break;
    case "progress":
      callbacks.onProgress?.({
        text: String(data.text || ""),
        node: data.node != null ? String(data.node) : undefined,
      });
      break;
    case "delta":
      callbacks.onDelta?.({ content: String(data.content || "") });
      break;
    case "done":
      callbacks.onDone?.({
        session_id: String(data.session_id || ""),
        answer: String(data.answer || ""),
        sources: Array.isArray(data.sources)
          ? (data.sources as RagSource[])
          : [],
        cache_hit: Boolean(data.cache_hit),
        knowledge_base_id:
          data.knowledge_base_id != null
            ? Number(data.knowledge_base_id)
            : undefined,
      });
      break;
    case "error":
      callbacks.onError?.(String(data.message || "流式请求失败"));
      break;
    default:
      break;
  }
};

/** SSE 流式问答 */
export const sendRagQueryStream = async (
  body: RagChatStreamBody,
  callbacks: RagStreamCallbacks,
  signal?: AbortSignal,
): Promise<void> => {
  const token = localStorage.getItem("access_token");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (token) headers.Authorization = `Bearer ${token}`;

  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);
  if (signal) {
    signal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/rag/chat/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    window.clearTimeout(timeoutId);
    if (error instanceof DOMException && error.name === "AbortError") {
      callbacks.onError?.("请求超时或已取消");
      return;
    }
    callbacks.onError?.("网络错误，请检查后端是否启动");
    return;
  }

  if (!response.ok) {
    window.clearTimeout(timeoutId);
    let detail = "请求失败";
    try {
      const errBody = await response.json();
      detail = errBody.detail || errBody.message || detail;
    } catch {
      /* ignore */
    }
    callbacks.onError?.(typeof detail === "string" ? detail : "请求失败");
    return;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    window.clearTimeout(timeoutId);
    callbacks.onError?.("浏览器不支持流式响应");
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        const payload = parseSSEBlock(block.trim());
        if (payload) dispatchSSE(payload, callbacks);
      }
    }
    if (buffer.trim()) {
      const payload = parseSSEBlock(buffer.trim());
      if (payload) dispatchSSE(payload, callbacks);
    }
  } finally {
    window.clearTimeout(timeoutId);
    reader.releaseLock();
  }
};
