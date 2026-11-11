/**
 * 智能问答 Tab
 *
 * 左栏：知识库选择 / 上传 / 文件列表（已有能力）
 * 右栏：会话列表 + SSE 对话 + 向量/BM25 权重滑条
 *
 * 约定（docs/rag-chat-memory-plan.md）
 * - 换知识库 = 换会话：localStorage 键 rag_session_id_{userId}_{kbId}
 * - SSE cache_hit 显示「缓存」角标
 * - 权限：admin 管 KB；同 unit 可上传删文件
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEventHandler,
} from "react";
import {
  Button,
  Col,
  Input,
  List,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Segmented,
  Select,
  Slider,
  Space,
  Tag,
  Typography,
  Upload,
  message,
} from "antd";
import ChatMarkdown from "@/components/ChatMarkdown";
import { formatAgentPhase } from "@/utils/agentPhase";
import { userFacingError } from "@/utils/userError";
import {
  ClearOutlined,
  DeleteOutlined,
  EyeOutlined,
  FolderOpenOutlined,
  PlusOutlined,
  SendOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import type { UploadProps } from "antd";
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  getKbCapabilities,
  listKnowledgeBases,
  type KbCapabilities,
  type KnowledgeBase,
} from "@/api/kb";
import {
  deleteFile,
  listFiles,
  uploadFiles,
  type FileMetadata,
} from "@/api/files";
import {
  clearRagSessions,
  clearStoredRagSessionId,
  createRagSession,
  deleteRagSession,
  getRagHistory,
  getStoredRagAnswerMode,
  getStoredRagSessionId,
  listRagSessions,
  saveRagAnswerMode,
  saveRagSessionId,
  sendRagQueryStream,
  type RagAnswerMode,
  type RagMessage,
  type RagSession,
  type RagSource,
} from "@/api/rag";
import FileDetailView from "./FileDetailView";

const { TextArea } = Input;
const { Text, Title } = Typography;

const ALLOWED = [".pdf", ".txt", ".md", ".docx"];
const KB_STORAGE_KEY = "agents_current_kb_id";

type UiMessage = {
  role: string;
  content: string;
  /** 流式中间过程（思考/检索预览）；终答写入 content 后可丢弃 */
  progress?: string;
  sources?: RagSource[] | null;
  cacheHit?: boolean;
  streaming?: boolean;
};

function extOf(name: string) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

function statusTag(status?: string | null) {
  const map: Record<string, { color: string; text: string }> = {
    pending: { color: "default", text: "排队" },
    processing: { color: "processing", text: "处理中" },
    processed: { color: "success", text: "完成" },
    failed: { color: "error", text: "失败" },
    rejected: { color: "warning", text: "拒绝" },
  };
  const m = map[status || ""] || { color: "default", text: status || "-" };
  return <Tag color={m.color}>{m.text}</Tag>;
}

function historyToUi(messages: RagMessage[]): UiMessage[] {
  return messages.map((m) => ({
    role: m.role,
    content: m.content,
    sources: m.sources || null,
  }));
}

export default function TabQa() {
  const [caps, setCaps] = useState<KbCapabilities | null>(null);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbId, setKbId] = useState<number | null>(null);
  const [files, setFiles] = useState<FileMetadata[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [detailFile, setDetailFile] = useState<FileMetadata | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const folderInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<number | null>(null);

  // ---- 对话态 ----
  const [sessions, setSessions] = useState<RagSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState("");
  /** 当前正在查看的会话是否在流式中（其它会话可并行跑，不互相 abort） */
  const [sending, setSending] = useState(false);
  const [phase, setPhase] = useState<string | null>(null);
  const [phaseTools, setPhaseTools] = useState<string[]>([]);
  /** 向量权重 0~1；BM25 = 1 - vector（滑条只暴露一个，另一侧自动补）；深度模式才调权 */
  const [vectorWeight, setVectorWeight] = useState(0.4);
  /** quick=快速(baseline+4万)；deep=深度(full+8万) */
  const [answerMode, setAnswerMode] = useState<RagAnswerMode>(() =>
    getStoredRagAnswerMode()
  );
  const chatEndRef = useRef<HTMLDivElement>(null);
  /** 各会话独立 AbortController：新建/切换会话不取消旧流 */
  const abortBySessionRef = useRef<Record<string, AbortController>>({});
  /** 会话消息缓存：切走再切回可看到进行中的气泡 */
  const messagesBySessionRef = useRef<Record<string, UiMessage[]>>({});
  const sessionIdRef = useRef<string | null>(null);
  const streamingIdsRef = useRef<Set<string>>(new Set());
  const [streamingTick, setStreamingTick] = useState(0);

  useEffect(() => {
    sessionIdRef.current = sessionId;
    setSending(sessionId != null && streamingIdsRef.current.has(sessionId));
  }, [sessionId, streamingTick]);

  const markStreaming = (sid: string, on: boolean) => {
    if (on) streamingIdsRef.current.add(sid);
    else {
      streamingIdsRef.current.delete(sid);
      delete abortBySessionRef.current[sid];
    }
    setStreamingTick((n) => n + 1);
  };

  const patchSessionMessages = (
    sid: string,
    updater: (prev: UiMessage[]) => UiMessage[],
  ) => {
    const prev = messagesBySessionRef.current[sid] || [];
    const next = updater(prev);
    messagesBySessionRef.current[sid] = next;
    if (sessionIdRef.current === sid) {
      setMessages(next);
    }
  };

  const currentKb = useMemo(
    () => kbs.find((k) => k.id === kbId) || null,
    [kbs, kbId],
  );

  const loadKbs = useCallback(async () => {
    const [list, c] = await Promise.all([listKnowledgeBases(), getKbCapabilities()]);
    setKbs(list);
    setCaps(c);
    const stored = Number(localStorage.getItem(KB_STORAGE_KEY) || 0);
    const pick =
      list.find((k) => k.id === stored) ||
      list.find((k) => k.is_default) ||
      list[0];
    setKbId(pick ? pick.id : null);
  }, []);

  const loadFiles = useCallback(async (id: number, quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const res = await listFiles(id);
      setFiles(res.items);
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  /** 加载某 KB 下会话；优先恢复 localStorage 中的 session */
  const loadSessionsForKb = useCallback(async (id: number) => {
    const list = await listRagSessions(id);
    setSessions(list);
    const stored = getStoredRagSessionId(id);
    const pick =
      (stored && list.find((s) => s.session_id === stored)) || list[0] || null;
    if (pick) {
      setSessionId(pick.session_id);
      saveRagSessionId(id, pick.session_id);
      const hist = await getRagHistory(pick.session_id);
      setMessages(historyToUi(hist.messages));
    } else {
      setSessionId(null);
      setMessages([]);
    }
  }, []);

  useEffect(() => {
    void loadKbs().catch(() => message.error("加载知识库失败"));
  }, [loadKbs]);

  useEffect(() => {
    if (kbId == null) {
      setFiles([]);
      setSessions([]);
      setSessionId(null);
      setMessages([]);
      return;
    }
    localStorage.setItem(KB_STORAGE_KEY, String(kbId));
    void loadFiles(kbId);
    void loadSessionsForKb(kbId).catch(() => {
      /* 会话接口失败时不挡文件区 */
    });
  }, [kbId, loadFiles, loadSessionsForKb]);

  useEffect(() => {
    if (kbId == null || detailFile) return;
    const busy = files.some(
      (f) => f.status === "pending" || f.status === "processing",
    );
    if (pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (!busy) return;
    pollRef.current = window.setInterval(() => {
      void loadFiles(kbId, true);
    }, 3000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [files, kbId, detailFile, loadFiles]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, phase]);

  const kbOptions = useMemo(
    () => kbs.map((k) => ({ value: k.id, label: `${k.name} (${k.file_count ?? 0})` })),
    [kbs],
  );

  const doUpload = async (fileList: File[]) => {
    if (kbId == null) {
      message.warning("请先选择知识库");
      return;
    }
    const valid: File[] = [];
    for (const f of fileList) {
      if (!ALLOWED.includes(extOf(f.name))) {
        message.warning(`跳过不支持格式: ${f.name}`);
        continue;
      }
      valid.push(f);
    }
    if (!valid.length) return;
    setUploading(true);
    try {
      const res = await uploadFiles(kbId, valid);
      if (res.batch_status === "success") message.success("上传成功，已入队处理");
      else if (res.batch_status === "partial_success")
        message.warning("部分文件成功入队");
      else message.error("全部失败");
      await loadFiles(kbId);
      await loadKbs();
    } catch {
      /* interceptor */
    } finally {
      setUploading(false);
    }
  };

  const uploadProps: UploadProps = {
    multiple: true,
    showUploadList: false,
    beforeUpload: () => false,
    onChange: (info) => {
      const list = info.fileList
        .map((f) => f.originFileObj)
        .filter(Boolean) as File[];
      if (list.length) void doUpload(list);
    },
  };

  const onFolderPick: ChangeEventHandler<HTMLInputElement> = (e) => {
    const list = Array.from(e.target.files || []);
    e.target.value = "";
    if (list.length) void doUpload(list);
  };

  const handleCreateKb = async () => {
    const name = newName.trim();
    if (!name) {
      message.warning("请输入名称");
      return;
    }
    try {
      const kb = await createKnowledgeBase({
        name,
        description: newDesc.trim() || undefined,
      });
      message.success("知识库已创建");
      setCreateOpen(false);
      setNewName("");
      setNewDesc("");
      await loadKbs();
      setKbId(kb.id);
    } catch {
      /* interceptor */
    }
  };

  const handleDeleteKb = async () => {
    if (kbId == null) return;
    try {
      clearStoredRagSessionId(kbId);
      await deleteKnowledgeBase(kbId);
      message.success("知识库已删除");
      setDetailFile(null);
      await loadKbs();
    } catch {
      /* interceptor */
    }
  };

  const handleDeleteFile = async (fid: number) => {
    try {
      await deleteFile(fid);
      message.success("已删除");
      if (kbId != null) await loadFiles(kbId);
      await loadKbs();
    } catch {
      /* interceptor */
    }
  };

  // ---- 会话操作 ----

  const handleNewSession = async () => {
    if (kbId == null) {
      message.warning("请先选择知识库");
      return;
    }
    try {
      // 不 abort 旧流：当前气泡写入会话袋，新会话独立展示
      const prevSid = sessionIdRef.current;
      if (prevSid) {
        messagesBySessionRef.current[prevSid] = messages;
      }
      const s = await createRagSession({ knowledge_base_id: kbId });
      saveRagSessionId(kbId, s.session_id);
      const welcome: UiMessage[] = s.welcome_hint
        ? [{ role: "assistant", content: s.welcome_hint }]
        : [];
      messagesBySessionRef.current[s.session_id] = welcome;
      setSessionId(s.session_id);
      setMessages(welcome);
      setPhase(null);
      setPhaseTools([]);
      const list = await listRagSessions(kbId);
      setSessions(list);
      message.success(
        prevSid && streamingIdsRef.current.has(prevSid)
          ? "已新建会话（原会话仍在后台生成）"
          : "已新建会话",
      );
    } catch {
      /* interceptor */
    }
  };

  const handleSelectSession = async (sid: string) => {
    if (kbId == null) return;
    const prevSid = sessionIdRef.current;
    if (prevSid) {
      messagesBySessionRef.current[prevSid] = messages;
    }
    setSessionId(sid);
    saveRagSessionId(kbId, sid);
    if (streamingIdsRef.current.has(sid) && messagesBySessionRef.current[sid]) {
      setMessages(messagesBySessionRef.current[sid]);
      return;
    }
    if (messagesBySessionRef.current[sid]?.length) {
      setMessages(messagesBySessionRef.current[sid]);
      if (!streamingIdsRef.current.has(sid)) {
        try {
          const hist = await getRagHistory(sid);
          const ui = historyToUi(hist.messages);
          messagesBySessionRef.current[sid] = ui;
          if (sessionIdRef.current === sid) setMessages(ui);
        } catch {
          /* keep cache */
        }
      }
      return;
    }
    try {
      const hist = await getRagHistory(sid);
      const ui = historyToUi(hist.messages);
      messagesBySessionRef.current[sid] = ui;
      setMessages(ui);
    } catch {
      setMessages([]);
    }
  };

  const handleDeleteSession = async (sid: string) => {
    if (kbId == null) return;
    try {
      abortBySessionRef.current[sid]?.abort();
      markStreaming(sid, false);
      delete messagesBySessionRef.current[sid];
      await deleteRagSession(sid);
      if (sessionId === sid) {
        clearStoredRagSessionId(kbId);
        setSessionId(null);
        setMessages([]);
      }
      await loadSessionsForKb(kbId);
      message.success("会话已删除");
    } catch {
      /* interceptor */
    }
  };

  const handleClearSessions = async () => {
    if (kbId == null) return;
    try {
      for (const ac of Object.values(abortBySessionRef.current)) {
        ac.abort();
      }
      abortBySessionRef.current = {};
      streamingIdsRef.current.clear();
      messagesBySessionRef.current = {};
      setStreamingTick((n) => n + 1);
      await clearRagSessions(kbId);
      clearStoredRagSessionId(kbId);
      setSessionId(null);
      setMessages([]);
      setSessions([]);
      message.success("已清空本知识库下全部会话");
    } catch {
      /* interceptor */
    }
  };

  const handleSend = async () => {
    const q = input.trim();
    if (!q || kbId == null) return;
    const viewSid = sessionId;
    if (viewSid && streamingIdsRef.current.has(viewSid)) return;

    setInput("");
    setPhase("retrieving");
    const baseMsgs = viewSid
      ? messagesBySessionRef.current[viewSid] || messages
      : messages;
    const seed: UiMessage[] = [
      ...baseMsgs,
      { role: "user", content: q },
      { role: "assistant", content: "", streaming: true },
    ];
    let ownerSid = viewSid || `__pending_${Date.now()}`;
    messagesBySessionRef.current[ownerSid] = seed;
    setMessages(seed);

    const ac = new AbortController();
    abortBySessionRef.current[ownerSid] = ac;
    markStreaming(ownerSid, true);

    let cacheHit = false;
    const viewingOwner = () => sessionIdRef.current === ownerSid;

    await sendRagQueryStream(
      {
        query: q,
        knowledge_base_id: kbId,
        session_id: viewSid,
        answer_mode: answerMode,
        vector_weight: vectorWeight,
        bm25_weight: Math.round((1 - vectorWeight) * 100) / 100,
      },
      {
        onMeta: (data) => {
          if (data.session_id) {
            const real = data.session_id;
            if (real !== ownerSid) {
              messagesBySessionRef.current[real] =
                messagesBySessionRef.current[ownerSid] || seed;
              delete messagesBySessionRef.current[ownerSid];
              const ctl = abortBySessionRef.current[ownerSid];
              if (ctl) abortBySessionRef.current[real] = ctl;
              delete abortBySessionRef.current[ownerSid];
              streamingIdsRef.current.delete(ownerSid);
              streamingIdsRef.current.add(real);
              ownerSid = real;
              setStreamingTick((n) => n + 1);
            }
            if (!sessionIdRef.current || sessionIdRef.current === ownerSid) {
              setSessionId(ownerSid);
              saveRagSessionId(kbId, ownerSid);
            }
          }
          if (data.cache_hit) cacheHit = true;
        },
        onStatus: (data) => {
          if (viewingOwner()) {
            setPhase(data.phase);
            setPhaseTools(data.tools || []);
          }
          if (data.cache_hit) cacheHit = true;
        },
        onProgress: (data) => {
          const piece = (data.text || "").trim();
          if (!piece) return;
          patchSessionMessages(ownerSid, (prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant" && last.streaming) {
              next[next.length - 1] = {
                ...last,
                progress: last.progress
                  ? `${last.progress}\n\n${piece}`
                  : piece,
              };
            }
            return next;
          });
        },
        onDelta: (data) => {
          const piece = data.content || "";
          patchSessionMessages(ownerSid, (prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant" && last.streaming) {
              next[next.length - 1] = {
                ...last,
                content: last.content + piece,
                progress: undefined,
                cacheHit,
              };
            }
            return next;
          });
        },
        onDone: (data) => {
          cacheHit = Boolean(data.cache_hit);
          const doneSid = data.session_id || ownerSid;
          patchSessionMessages(doneSid, (prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") {
              next[next.length - 1] = {
                role: "assistant",
                content: data.answer || last.content,
                sources: data.sources || null,
                cacheHit,
                streaming: false,
                progress: undefined,
              };
            }
            return next;
          });
          markStreaming(doneSid, false);
          markStreaming(ownerSid, false);
          if (viewingOwner() || sessionIdRef.current === doneSid) {
            setPhase(null);
            setPhaseTools([]);
          }
          void listRagSessions(kbId).then(setSessions).catch(() => undefined);
        },
        onError: (msg) => {
          const shown = userFacingError(msg);
          if (viewingOwner()) message.error(shown);
          patchSessionMessages(ownerSid, (prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant" && last.streaming) {
              next[next.length - 1] = {
                role: "assistant",
                content: last.content || shown,
                streaming: false,
              };
            }
            return next;
          });
          markStreaming(ownerSid, false);
          if (viewingOwner()) {
            setPhase(null);
            setPhaseTools([]);
          }
        },
      },
      ac.signal,
    );

    markStreaming(ownerSid, false);
    if (viewingOwner()) {
      setPhase(null);
      setPhaseTools([]);
    }
  };

  if (detailFile) {
    return (
      <Row gutter={16} style={{ height: "100%" }}>
        <Col span={24} style={{ height: "100%" }}>
          <div
            style={{
              height: "100%",
              background: "#fff",
              borderRadius: 12,
              padding: 16,
              boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
            }}
          >
            <FileDetailView
              fileId={detailFile.id}
              filename={detailFile.original_filename}
              onBack={() => {
                setDetailFile(null);
                if (kbId != null) void loadFiles(kbId);
              }}
            />
          </div>
        </Col>
      </Row>
    );
  }

  return (
    <Row gutter={16} style={{ height: "100%" }}>
      {/* -------- 左：知识库 + 文件 -------- */}
      <Col span={8} style={{ height: "100%" }}>
        <div
          style={{
            height: "100%",
            background: "#fff",
            borderRadius: 12,
            padding: 16,
            display: "flex",
            flexDirection: "column",
            gap: 12,
            boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
          }}
        >
          <Title level={5} style={{ margin: 0 }}>
            知识库
          </Title>

          <Space.Compact style={{ width: "100%" }}>
            <Select
              style={{ flex: 1 }}
              placeholder="选择知识库"
              options={kbOptions}
              value={kbId ?? undefined}
              onChange={(v) => setKbId(v)}
            />
            {caps?.can_create_kb && (
              <Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)} />
            )}
          </Space.Compact>

          <Space wrap align="center">
            <Upload {...uploadProps} disabled={uploading || kbId == null}>
              <Button icon={<UploadOutlined />} loading={uploading} disabled={kbId == null}>
                上传
              </Button>
            </Upload>
            <Button
              icon={<FolderOpenOutlined />}
              disabled={uploading || kbId == null}
              onClick={() => folderInputRef.current?.click()}
            >
              文件夹
            </Button>
            {caps?.can_delete_kb && kbId != null && (
              <Popconfirm
                title="删除该知识库及全部文件与向量？"
                onConfirm={() => void handleDeleteKb()}
              >
                <Button type="link" danger size="small" icon={<DeleteOutlined />}>
                  删除知识库
                </Button>
              </Popconfirm>
            )}
            <input
              ref={folderInputRef}
              type="file"
              multiple
              style={{ display: "none" }}
              // @ts-expect-error webkitdirectory
              webkitdirectory=""
              onChange={onFolderPick}
            />
          </Space>

          <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
            <List
              loading={loading}
              size="small"
              dataSource={files}
              locale={{ emptyText: kbId ? "暂无文件" : "请选择知识库" }}
              renderItem={(item) => (
                <List.Item
                  actions={[
                    <Button
                      key="view"
                      type="link"
                      size="small"
                      icon={<EyeOutlined />}
                      onClick={() => setDetailFile(item)}
                    />,
                    <Popconfirm
                      key="del"
                      title="删除文件（库+向量）？"
                      onConfirm={() => void handleDeleteFile(item.id)}
                    >
                      <Button type="link" size="small" danger icon={<DeleteOutlined />} />
                    </Popconfirm>,
                  ]}
                >
                  <List.Item.Meta
                    title={
                      <div style={{ paddingRight: 8 }}>
                        <Text
                          style={{
                            display: "block",
                            whiteSpace: "normal",
                            wordBreak: "break-all",
                            lineHeight: 1.4,
                          }}
                        >
                          {item.original_filename}
                        </Text>
                        <div style={{ marginTop: 4 }}>{statusTag(item.status)}</div>
                      </div>
                    }
                    description={
                      item.status === "processing" || item.status === "pending" ? (
                        <Progress
                          percent={item.status === "pending" ? 5 : 50}
                          size="small"
                          showInfo={false}
                        />
                      ) : (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          切片 {item.total_chunks ?? item.chunks_count ?? 0} · 向量{" "}
                          {item.embedded_chunks ?? 0}
                        </Text>
                      )
                    }
                  />
                </List.Item>
              )}
            />
          </div>
        </div>
      </Col>

      {/* -------- 右：会话 + 对话 -------- */}
      <Col span={16} style={{ height: "100%" }}>
        <div
          style={{
            height: "100%",
            background: "#fff",
            borderRadius: 12,
            padding: 16,
            display: "flex",
            flexDirection: "column",
            gap: 10,
            boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <Space wrap>
              <Title level={5} style={{ margin: 0 }}>
                对话
              </Title>
              {currentKb && (
                <Tag color="green">{currentKb.name}</Tag>
              )}
              {phase && (
                <Tag color="processing">
                  {formatAgentPhase(phase, phaseTools)}…
                </Tag>
              )}
            </Space>
            <Space wrap>
              <Button size="small" icon={<PlusOutlined />} onClick={() => void handleNewSession()}>
                新会话
              </Button>
              <Popconfirm
                title="清空本知识库下全部会话？"
                onConfirm={() => void handleClearSessions()}
                disabled={kbId == null}
              >
                <Button size="small" icon={<ClearOutlined />} disabled={kbId == null}>
                  清空
                </Button>
              </Popconfirm>
            </Space>
          </div>

          {/* 会话选择 */}
          <Select
            style={{ width: "100%" }}
            placeholder={kbId ? "选择或新建会话" : "请先选择知识库"}
            value={sessionId ?? undefined}
            disabled={kbId == null}
            options={sessions.map((s) => ({
              value: s.session_id,
              label: s.title || s.session_id.slice(0, 8),
            }))}
            onChange={(v) => void handleSelectSession(v)}
            optionRender={(option) => (
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                  {option.label}
                </span>
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={(e) => {
                    e.stopPropagation();
                    void handleDeleteSession(String(option.value));
                  }}
                />
              </div>
            )}
          />

          {/* 快速 / 深度 */}
          <div style={{ padding: "0 4px 8px" }}>
            <Segmented
              block
              disabled={sending}
              value={answerMode}
              onChange={(v) => {
                const next = v === "quick" ? "quick" : "deep";
                setAnswerMode(next);
                saveRagAnswerMode(next);
              }}
              options={[
                { label: "快速回答", value: "quick" },
                { label: "深度思考", value: "deep" },
              ]}
            />
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 6 }}>
              {answerMode === "quick"
                ? "Baseline：向量15 + 相似15 → 重排取10 · 预算约4万 token · ReAct"
                : "Full：扩写+混合检索 · 预算约8万 token · ReAct（强弱模型）"}
            </Text>
          </div>

          {/* 权重：仅深度思考可调；快速走 baseline 固定双路 */}
          {answerMode === "deep" ? (
            <div style={{ padding: "0 4px" }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                检索权重 · 向量 {Math.round(vectorWeight * 100)}% / BM25{" "}
                {Math.round((1 - vectorWeight) * 100)}%
              </Text>
              <Slider
                min={0}
                max={1}
                step={0.05}
                value={vectorWeight}
                onChange={(v) => setVectorWeight(Number(v))}
                tooltip={{
                  formatter: (v) => `向量 ${Math.round(Number(v) * 100)}%`,
                }}
                disabled={sending}
              />
            </div>
          ) : null}

          {/* 消息列表 */}
          <div
            style={{
              flex: 1,
              minHeight: 0,
              overflow: "auto",
              background: "#f7faf9",
              borderRadius: 8,
              padding: 12,
            }}
          >
            {messages.length === 0 ? (
              <div style={{ textAlign: "center", paddingTop: 48 }}>
                <Text type="secondary">选择或新建会话后开始提问</Text>
                <br />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  问答将基于当前知识库检索；换库会切换会话
                </Text>
              </div>
            ) : (
              messages.map((m, idx) => (
                <div
                  key={idx}
                  style={{
                    marginBottom: 12,
                    display: "flex",
                    flexDirection: "column",
                    alignItems: m.role === "user" ? "flex-end" : "flex-start",
                  }}
                >
                  <Space size={4} style={{ marginBottom: 4 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {m.role === "user" ? "我" : "助手"}
                    </Text>
                    {m.cacheHit && <Tag color="blue">缓存</Tag>}
                  </Space>
                  <div
                    style={{
                      maxWidth: "85%",
                      padding: "8px 12px",
                      borderRadius: 10,
                      background: m.role === "user" ? "#1a6b5c" : "#fff",
                      color: m.role === "user" ? "#fff" : "#1f1f1f",
                      wordBreak: "break-word",
                      boxShadow:
                        m.role === "user" ? "none" : "0 1px 2px rgba(0,0,0,0.06)",
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
                  {!!m.sources?.length && (
                    <details style={{ marginTop: 4, maxWidth: "85%", fontSize: 12 }}>
                      <summary style={{ cursor: "pointer", color: "#666" }}>
                        来源 {m.sources.length}
                      </summary>
                      <ul style={{ paddingLeft: 16, margin: "4px 0" }}>
                        {m.sources.slice(0, 5).map((s, i) => (
                          <li key={i} style={{ color: "#555" }}>
                            {(s.content || "").slice(0, 120)}
                            {s.content && s.content.length > 120 ? "…" : ""}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              ))
            )}
            <div ref={chatEndRef} />
          </div>

          <Space.Compact style={{ width: "100%" }}>
            <TextArea
              rows={2}
              placeholder={kbId ? "输入问题，Enter 发送（Shift+Enter 换行）" : "请先选择知识库"}
              value={input}
              disabled={kbId == null || sending}
              style={{ resize: "none" }}
              onChange={(e) => setInput(e.target.value)}
              onPressEnter={(e) => {
                if (!e.shiftKey) {
                  e.preventDefault();
                  void handleSend();
                }
              }}
            />
            <Button
              type="primary"
              icon={<SendOutlined />}
              loading={sending}
              disabled={kbId == null || !input.trim()}
              style={{ height: "auto" }}
              onClick={() => void handleSend()}
            >
              发送
            </Button>
          </Space.Compact>
        </div>
      </Col>

      <Modal
        title="新建知识库"
        open={createOpen}
        onOk={() => void handleCreateKb()}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input
            placeholder="名称"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            maxLength={128}
          />
          <Input.TextArea
            placeholder="描述（可选）"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            maxLength={512}
            rows={3}
          />
        </Space>
      </Modal>
    </Row>
  );
}
