/** LangGraph SSE status.phase → 中文文案（TabQa / TabAgent 共用） */

const PHASE_LABEL: Record<string, string> = {
  preparing: "准备中",
  thinking: "思考中",
  planning: "规划中",
  retrieving: "检索中",
  acting: "调用工具",
  reviewing: "检查结果",
  generating: "生成中",
  waiting_user: "等待补充",
};

export function formatAgentPhase(
  phase: string | null | undefined,
  tools?: string[] | null,
): string {
  if (!phase) return "";
  const base = PHASE_LABEL[phase] || phase;
  const suffix = tools?.length ? `（${tools.join("、")}）` : "";
  return `${base}${suffix}`;
}
