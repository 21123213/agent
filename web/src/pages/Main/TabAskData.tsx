/**
 * 智能问数 Tab
 *
 * - 会话列表 + SSE 对话
 * - done 事件可展示 route / SQL 预览
 * - 不绑定知识库（agent_type=askdata）
 */
import { useEffect, useRef, useState } from "react";
import {
  Button,
  Input,
  List,
  Space,
  Tag,
  Typography,
  message,
  Empty,
} from "antd";
import { PlusOutlined, SendOutlined, DeleteOutlined } from "@ant-design/icons";
import ChatMarkdown from "@/components/ChatMarkdown";
import AskdataErGraph from "@/components/AskdataErGraph";
import { getUserInfo } from "@/utils/auth";
import { userFacingError } from "@/utils/userError";
import {
  clearStoredAskdataSessionId,
  createAskdataSession,
  deleteAskdataSession,
  getAskdataHistory,
  getStoredAskdataSessionId,
  listAskdataSessions,
  saveAskdataSessionId,
  streamAskdataChat,
  type AskdataSession,
} from "@/api/askdata";

const { Text, Title } = Typography;
const { TextArea } = Input;

interface Bubble {
  role: "user" | "assistant";
  content: string;
  route?: string;
  denied?: boolean;
}

export default function TabAskData() {
  const user = getUserInfo();
  const userId = user?.id ?? "anon";

  const [sessions, setSessions] = useState<AskdataSession[]>([]);
  const [sessionId, setSessionId] = useState(
    () => getStoredAskdataSessionId(userId)
  );
  const [messages, setMessages] = useState<Bubble[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const reloadSessions = async () => {
    try {
      setSessions((await listAskdataSessions()) || []);
    } catch {
      /* interceptor */
    }
  };

  const loadHistory = async (sid: string) => {
    if (!sid) {
      setMessages([]);
      return;
    }
    try {
      const hist = await getAskdataHistory(sid);
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
      saveAskdataSessionId(userId, sessionId);
      loadHistory(sessionId);
    }
  }, [sessionId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleNewSession = async () => {
    try {
      const s = await createAskdataSession("问数会话");
      setSessionId(s.session_id);
      await reloadSessions();
      await loadHistory(s.session_id);
      message.success("已新建问数会话");
    } catch {
      /* */
    }
  };

  const handleDeleteSession = async () => {
    if (!sessionId) return;
    try {
      await deleteAskdataSession(sessionId);
      clearStoredAskdataSessionId(userId);
      setSessionId("");
      setMessages([]);
      await reloadSessions();
      message.success("已删除");
    } catch {
      /* */
    }
  };

  const handleSend = async () => {
    const q = input.trim();
    if (!q || sending) return;
    setInput("");
    setSending(true);
    setPhase("querying");
    setMessages((prev) => [...prev, { role: "user", content: q }]);
    setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

    const ac = new AbortController();
    abortRef.current = ac;
    let acc = "";
    let metaSid = sessionId;
    let route: string | undefined;
    let denied = false;

    try {
      await streamAskdataChat(
        { query: q, session_id: sessionId || undefined },
        (event, data) => {
          if (event === "meta" && data.session_id) {
            metaSid = String(data.session_id);
            setSessionId(metaSid);
          }
          if (event === "status") {
            setPhase(String(data.phase || "querying"));
          }
          if (event === "delta" && data.content) {
            acc += String(data.content);
            setMessages((prev) => {
              const copy = [...prev];
              copy[copy.length - 1] = {
                role: "assistant",
                content: acc,
                route,
                denied,
              };
              return copy;
            });
          }
          if (event === "done") {
            route = data.route ? String(data.route) : route;
            denied = Boolean(data.denied);
            setMessages((prev) => {
              const copy = [...prev];
              copy[copy.length - 1] = {
                role: "assistant",
                content: acc || copy[copy.length - 1]?.content || "",
                route,
                denied,
              };
              return copy;
            });
            reloadSessions();
          }
          if (event === "error") {
            const msg = userFacingError(data.message || "问数失败");
            setMessages((prev) => {
              const copy = [...prev];
              copy[copy.length - 1] = {
                role: "assistant",
                content: msg,
                denied: true,
              };
              return copy;
            });
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
      abortRef.current = null;
    }
  };

  return (
    <div style={{ display: "flex", height: "100%", gap: 16, minHeight: 0 }}>
      <div
        style={{
          width: 220,
          flexShrink: 0,
          background: "#fff",
          borderRadius: 8,
          padding: 12,
          display: "flex",
          flexDirection: "column",
          minHeight: 0,
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
            icon={<DeleteOutlined />}
            disabled={!sessionId}
            onClick={handleDeleteSession}
          />
        </Space>
        <List
          size="small"
          style={{ flex: 1, overflow: "auto" }}
          dataSource={sessions}
          locale={{ emptyText: <Empty description="暂无会话" /> }}
          renderItem={(item) => (
            <List.Item
              style={{
                cursor: "pointer",
                background:
                  item.session_id === sessionId ? "#e8f5f1" : undefined,
                borderRadius: 6,
                padding: "8px 10px",
              }}
              onClick={() => setSessionId(item.session_id)}
            >
              <Text ellipsis style={{ width: "100%" }}>
                {item.title || "问数会话"}
              </Text>
            </List.Item>
          )}
        />
      </div>

      <div
        style={{
          flex: 1,
          minWidth: 0,
          background: "#fff",
          borderRadius: 8,
          display: "flex",
          flexDirection: "column",
          padding: 16,
          minHeight: 0,
        }}
      >
        <Title level={5} style={{ marginTop: 0 }}>
          智能问数
          {phase ? (
            <Tag color="processing" style={{ marginLeft: 8 }}>
              {phase}
            </Tag>
          ) : null}
        </Title>
        <AskdataErGraph />
        <Text type="secondary" style={{ marginBottom: 8 }}>
          可问今日/本月销售额、按门店汇总、退款；复杂题走多表 SQL。仅只读。
        </Text>
        <div style={{ flex: 1, overflow: "auto", marginBottom: 12 }}>
          {messages.length === 0 ? (
            <Empty description="新建会话后开始提问" />
          ) : (
            messages.map((m, i) => (
              <div
                key={i}
                style={{
                  marginBottom: 12,
                  textAlign: m.role === "user" ? "right" : "left",
                }}
              >
                <div
                  style={{
                    display: "inline-block",
                    maxWidth: "85%",
                    textAlign: "left",
                    background: m.role === "user" ? "#d9f2ec" : "#f5f7f6",
                    borderRadius: 8,
                    padding: "8px 12px",
                    wordBreak: "break-word",
                  }}
                >
                  {m.route ? (
                    <div style={{ marginBottom: 4 }}>
                      <Tag>{m.route}</Tag>
                      {m.denied ? <Tag color="warning">受限</Tag> : null}
                    </div>
                  ) : null}
                  {m.role === "assistant" ? (
                    <ChatMarkdown
                      content={
                        m.content ||
                        (sending && i === messages.length - 1 ? "…" : "")
                      }
                    />
                  ) : (
                    <span style={{ whiteSpace: "pre-wrap" }}>{m.content}</span>
                  )}
                </div>
              </div>
            ))
          )}
          <div ref={endRef} />
        </div>
        <Space.Compact style={{ width: "100%" }}>
          <TextArea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="例如：今日销售额 / 本月按门店销售额 / 退款金额按品牌汇总"
            autoSize={{ minRows: 1, maxRows: 4 }}
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
          >
            发送
          </Button>
        </Space.Compact>
      </div>
    </div>
  );
}
