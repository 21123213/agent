/** 与后端 app.common.user_errors 对齐的前端兜底文案 */
export const PUBLIC_BUSY_MESSAGE = "系统繁忙，请联系管理员";

const INFRA_HINT =
  /Error code|Traceback|model_not_found|invalid_request_error|APIConnectionError|ConnectTimeout|ReadTimeout|httpx\.|openai\.|Connection refused|Max retries|SSLError|ECONNREFUSED|status_code|"type"\s*:\s*"invalid/i;

/**
 * 聊天气泡 / toast 对外文案。
 * 原始技术细节打到 console，便于本地排查。
 */
export function userFacingError(raw: unknown, fallback = PUBLIC_BUSY_MESSAGE): string {
  const text = String(raw ?? "").trim();
  if (!text) return fallback;
  if (INFRA_HINT.test(text) || (text.length > 240 && (text.includes("{") || text.includes("\n")))) {
    // eslint-disable-next-line no-console
    console.error("[userFacingError] masked infra error:", text);
    return fallback;
  }
  // 后端已包装的文案直接用
  if (text.includes("联系管理员") || text.length <= 200) {
    return text;
  }
  // eslint-disable-next-line no-console
  console.error("[userFacingError] truncated long message:", text);
  return fallback;
}
