/**
 * 推理 Agent 工作区：模式切换、会话、聊天、缺参续跑。
 * 无运行日志面板（观测看 LangSmith / Jaeger）。
 */
import { useEffect, useRef, useState } from "react";
import {
  Button,
  Input,
  List,
  Segmented,
  Space,
  Typography,
  message,
  Empty,
} from "antd";
import { ClearOutlined, PlusOutlined, SendOutlined } from "@ant-design/icons";
import ChatMarkdown from "@/components/ChatMarkdown";
import { formatAgentPhase } from "@/utils/agentPhase";
import { getUserInfo } from "@/utils/auth";
import { userFacingError } from "@/utils/userError";
import {
  clearStoredReasonerSessionId,
  createReasonerSession,
  deleteReasonerSession,
  getReasonerHistory,
  getStoredReasonerMode,
  getStoredReasonerSessionId,
  listReasonerSessions,
  saveReasonerMode,
  saveReasonerSessionId,
  streamReasonerChat,
  type ReasonerMode,
  type ReasonerSession,
} from "@/api/reasoner";

const { Text, Title, Paragraph } = Typography;
const { TextArea } = Input;

interface Bubble {
  role: "user" | "assistant" | "system";
  content: string;
  progress?: string;
  streaming?: boolean;
}

export default function TabAgent() {
  const user = getUserInfo();
  const userId = user?.id ?? "anon";

  const [mode, setMode] = useState<ReasonerMode>(() => getStoredReasonerMode());
  const [sessions, setSessions] = useState<ReasonerSession[]>([]);
  const [sessionId, setSessionId] = useState(
    () => getStoredReasonerSessionId(userId)
  );
  const [messages, setMessages] = useState<Bubble[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [waitingUser, setWaitingUser] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [phaseTools, setPhaseTools] = useState<string[]>([]);
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const reloadSessions = async () => {
    try {
      const rows = await listReasonerSessions();
      setSessions(rows || []);
    } catch {
      /* interceptor 已提示 */
    }
  };

  const loadHistory = async (sid: string) => {
    if (!sid) {
      setMessages([]);
      return;
    }
    try {
      const hist = await getReasonerHistory(sid);
      setMessages(
        (hist.messages || []).map((m) => ({
          role: (m.role as Bubble["role"]) || "assistant",
          content: m.content || "",
        }))
      );
    } catch {
      setMessages([]);
    }
  };

  useEffect(() => {
    reloadSessions();
  }, []);

  useEffect(() => {
    if (sessionId) {
      saveReasonerSessionId(userId, sessionId);
      loadHistory(sessionId);
    }
  }, [sessionId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleNewSession = async () => {
    try {
      const s = await createReasonerSession("推理会话");
      setSessionId(s.session_id);
      setWaitingUser(false);
      await reloadSessions();
      await loadHistory(s.session_id);
      message.success("已新建会话");
    } catch {
      /* */
    }
  };

  const handleDeleteSession = async () => {
    if (!sessionId) return;
    try {
      await deleteReasonerSession(sessionId);
      clearStoredReasonerSessionId(userId);
      setSessionId("");
      setMessages([]);
      setWaitingUser(false);
      await reloadSessions();
      message.success("已删除会话");
    } catch {
      /* */
    }
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;

    setSending(true);
    setInput("");
    setPhase("preparing");
    setPhaseTools([]);

    // 展示用户气泡
    setMessages((prev) => [...prev, { role: "user", content: text }]);

    let assistantAcc = "";
    // 注意：不要闭包依赖 React 的 sending（调用时仍是旧值 false），否则会不断 append 新气泡。
    const pushAssistant = (chunk: string, replace = false) => {
      assistantAcc = replace ? chunk : assistantAcc + chunk;
      setMessages((prev) => {
        const copy = [...prev];
        const last = copy[copy.length - 1];
        if (last?.role === "assistant") {
          copy[copy.length - 1] = {
            role: "assistant",
            content: assistantAcc,
            streaming: false,
          };
          return copy;
        }
        return [
          ...copy,
          { role: "assistant", content: assistantAcc, streaming: false },
        ];
      });
    };

    const ac = new AbortController();
    abortRef.current = ac;

    try {
      // 先占位一条助手消息（空内容 + streaming，气泡内显示阶段文案）
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "", streaming: true },
      ]);

      await streamReasonerChat(
        {
          query: text,
          session_id: sessionId || undefined,
          mode,
          resume: waitingUser
            ? { resume_token: sessionId, answers: text }
            : undefined,
        },
        (event, data) => {
          if (event === "meta" && data.session_id) {
            const sid = String(data.session_id);
            setSessionId(sid);
            saveReasonerSessionId(userId, sid);
          }
          if (event === "status" && data.phase) {
            setPhase(String(data.phase));
            const tools = Array.isArray(data.tools)
              ? (data.tools as unknown[]).map(String)
              : [];
            setPhaseTools(tools);
          }
          if (event === "progress" && data.text) {
            const piece = String(data.text).trim();
            if (piece) {
              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant" && last.streaming) {
                  copy[copy.length - 1] = {
                    ...last,
                    progress: last.progress
                      ? `${last.progress}\n\n${piece}`
                      : piece,
                  };
                }
                return copy;
              });
            }
          }
          if (event === "delta" && data.content) {
            pushAssistant(String(data.content), true);
          }
          if (event === "needs_input") {
            setWaitingUser(true);
            const prompt =
              (data.prompt as string) ||
              "请补充必要信息后发送。";
            pushAssistant(prompt, true);
            message.info("需要你补充必要信息");
          }
          if (event === "done") {
            if (data.status === "ok") {
              setWaitingUser(false);
              // 后端 delta 与 done.answer 可能同文；push 会更新同一条气泡，不会再 append
              if (data.answer) pushAssistant(String(data.answer), true);
            }
            if (data.status === "needs_input") {
              setWaitingUser(true);
            }
            reloadSessions();
          }
          if (event === "error") {
            const shown = userFacingError(data.message || "推理失败");
            message.error(shown);
            pushAssistant(shown, true);
            setWaitingUser(false);
          }
        },
        ac.signal
      );
    } catch (e: unknown) {
      if ((e as Error)?.name !== "AbortError") {
        message.error((e as Error)?.message || "请求失败");
      }
    } finally {
      setSending(false);
      setPhase(null);
      setPhaseTools([]);
      setMessages((prev) => {
        const copy = [...prev];
        const last = copy[copy.length - 1];
        if (last?.role === "assistant" && last.streaming) {
          copy[copy.length - 1] = {
            ...last,
            content: last.content || "（无回复）",
            streaming: false,
          };
        }
        return copy;
      });
      abortRef.current = null;
    }
  };

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        gap: 16,
        background: "#fff",
        borderRadius: 12,
        padding: 16,
        boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
      }}
    >
      {/* 左侧会话列表 */}
      <div
        style={{
          width: 220,
          borderRight: "1px solid #f0f0f0",
          paddingRight: 12,
          display: "flex",
          flexDirection: "column",
        }}
      >
        <Space style={{ marginBottom: 8 }}>
          <Button
            type="primary"
            size="small"
            icon={<PlusOutlined />}
            onClick={handleNewSession}
          >
            新建
          </Button>
          <Button
            size="small"
            danger
            icon={<ClearOutlined />}
            disabled={!sessionId}
            onClick={handleDeleteSession}
          >
            删除
          </Button>
        </Space>
        <List
          size="small"
          dataSource={sessions}
          locale={{ emptyText: <Empty description="暂无会话" /> }}
          renderItem={(item) => (
            <List.Item
              style={{
                cursor: "pointer",
                background:
                  item.session_id === sessionId ? "#f0faf7" : undefined,
                padding: "8px 8px",
                borderRadius: 6,
              }}
              onClick={() => {
                setSessionId(item.session_id);
                setWaitingUser(false);
              }}
            >
              <Text ellipsis style={{ maxWidth: 180 }}>
                {item.title || item.session_id.slice(0, 8)}
              </Text>
            </List.Item>
          )}
        />
      </div>

      {/* 右侧对话 */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 8,
          }}
        >
          <Title level={4} style={{ margin: 0 }}>
            推理 Agent
          </Title>
          <Segmented
            value={mode}
            disabled={sending}
            options={[
              { label: "ReAct", value: "react" },
              { label: "Plan-Execute", value: "plan_execute" },
            ]}
            onChange={(v) => {
              const next = v === "plan_execute" ? "plan_execute" : "react";
              setMode(next);
              saveReasonerMode(next);
            }}
          />
        </div>
        {(phase || waitingUser) && (
          <Text type="secondary" style={{ fontSize: 12, marginBottom: 4 }}>
            {phase ? `阶段：${formatAgentPhase(phase, phaseTools)}` : ""}
            {waitingUser ? " · 等待你补充" : ""}
          </Text>
        )}

        <div
          style={{
            flex: 1,
            overflow: "auto",
            padding: "8px 4px",
            background: "#fafafa",
            borderRadius: 8,
            marginBottom: 12,
          }}
        >
          {messages.length === 0 && (
            <Paragraph type="secondary" style={{ padding: 16 }}>
              选择或新建会话后开始。需要你提供的必要信息时，会在对话里提示，直接回复即可续跑。
            </Paragraph>
          )}
          {messages.map((m, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                justifyContent: m.role === "user" ? "flex-end" : "flex-start",
                marginBottom: 10,
              }}
            >
              <div
                style={{
                  maxWidth: "75%",
                  padding: "8px 12px",
                  borderRadius: 10,
                  background: m.role === "user" ? "#1a6b5c" : "#fff",
                  color: m.role === "user" ? "#fff" : "#222",
                  border: m.role === "user" ? "none" : "1px solid #eee",
                  wordBreak: "break-word",
                }}
              >
                {m.role === "assistant" ? (
                  m.streaming && !m.content ? (
                    <div>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {formatAgentPhase(phase, phaseTools) || "处理中"}…
                      </Text>
                      {m.progress ? (
                        <div
                          style={{
                            marginTop: 8,
                            whiteSpace: "pre-wrap",
                            color: "#595959",
                            fontSize: 13,
                            lineHeight: 1.55,
                          }}
                        >
                          {m.progress}
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <ChatMarkdown content={m.content} />
                  )
                ) : (
                  <span style={{ whiteSpace: "pre-wrap" }}>{m.content}</span>
                )}
              </div>
            </div>
          ))}
          <div ref={endRef} />
        </div>

        <Space.Compact style={{ width: "100%" }}>
          <TextArea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={
              waitingUser ? "补充必要信息…" : "输入问题，可让我用工具计算/查时间…"
            }
            autoSize={{ minRows: 2, maxRows: 4 }}
            onPressEnter={(e) => {
              if (!e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            disabled={sending}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            loading={sending}
            onClick={handleSend}
            style={{ height: "auto" }}
          >
            发送
          </Button>
        </Space.Compact>
      </div>
    </div>
  );
}
