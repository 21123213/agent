/**
 * 文档处理详情：进度轮询 + 切片浏览器（对齐旧前端 ViewChunksTab）。
 * 轮询仅打 /status；切片在数量变化或完成时再拉，避免每秒刷 /chunks。
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Drawer,
  Empty,
  Input,
  List,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { ArrowLeftOutlined, FileTextOutlined, SearchOutlined } from "@ant-design/icons";
import { getFileChunks, getFileStatus, type ProcessingStatus } from "@/api/files";

const { Text, Title, Paragraph } = Typography;
const { Search } = Input;
const POLL_MS = 2500;

type ChunkItem = {
  chunk_index: number;
  chunk_text?: string | null;
  char_count?: number;
  page_numbers?: number[];
  content_types?: string[];
  element_types?: string[];
  element_count?: number;
  element_ids?: Array<number | string>;
  is_embedded?: boolean;
  vector_id?: string | null;
};

type Props = {
  fileId: number;
  filename: string;
  onBack: () => void;
};

function statusColor(s?: string) {
  if (s === "processed" || s === "completed") return "success";
  if (s === "failed") return "error";
  if (s === "processing" || s === "pending") return "processing";
  return "default";
}

function highlightText(text: string, query: string): ReactNode {
  const q = query.trim();
  if (!q || !text) return text;
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  const parts: ReactNode[] = [];
  let start = 0;
  let idx = lower.indexOf(needle, start);
  let key = 0;
  while (idx !== -1) {
    if (idx > start) parts.push(text.slice(start, idx));
    parts.push(
      <mark
        key={`h-${key++}`}
        style={{
          background: "#ffe58f",
          color: "inherit",
          padding: "0 1px",
          borderRadius: 2,
        }}
      >
        {text.slice(idx, idx + needle.length)}
      </mark>,
    );
    start = idx + needle.length;
    idx = lower.indexOf(needle, start);
  }
  if (start < text.length) parts.push(text.slice(start));
  return parts.length ? <>{parts}</> : text;
}

function getChunkTypes(chunk: ChunkItem): string[] {
  const merged = [
    ...(chunk.element_types || []),
    ...(chunk.content_types || []),
  ];
  return Array.from(new Set(merged.filter(Boolean)));
}

export default function FileDetailView({ fileId, filename, onBack }: Props) {
  const [status, setStatus] = useState<ProcessingStatus | null>(null);
  const [chunks, setChunks] = useState<ChunkItem[]>([]);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchText, setSearchText] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [selected, setSelected] = useState<ChunkItem | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const doneRef = useRef(false);
  const lastChunkTotalRef = useRef(-1);
  const fileIdRef = useRef(fileId);
  fileIdRef.current = fileId;

  const loadChunks = useCallback(async (fid: number, quiet = false) => {
    if (!quiet) setChunksLoading(true);
    try {
      const ch = await getFileChunks(fid);
      setChunks((ch.items || []) as ChunkItem[]);
    } finally {
      if (!quiet) setChunksLoading(false);
    }
  }, []);

  const pollStatus = useCallback(async () => {
    const fid = fileIdRef.current;
    try {
      const st = await getFileStatus(fid);
      setStatus(st);
      setError(null);

      const total = st.chunking?.total_chunks ?? 0;
      const done =
        st.status === "processed" ||
        st.status === "completed" ||
        st.status === "failed";

      // 切片数变化才拉列表；完成时再拉一次（刷新 is_embedded）
      if (total > 0 && total !== lastChunkTotalRef.current) {
        const quiet = lastChunkTotalRef.current > 0;
        lastChunkTotalRef.current = total;
        await loadChunks(fid, quiet);
      }
      if (done) {
        if (total > 0) await loadChunks(fid, true);
        lastChunkTotalRef.current = total;
        doneRef.current = true;
      }
    } catch (e: any) {
      setError(e?.response?.data?.detail || e?.message || "加载失败");
    }
  }, [loadChunks]);

  useEffect(() => {
    doneRef.current = false;
    lastChunkTotalRef.current = -1;
    setStatus(null);
    setChunks([]);
    setSearchText("");
    setTypeFilter("all");
    setSelected(null);
    setDrawerOpen(false);

    void pollStatus();
    const timer = window.setInterval(() => {
      if (doneRef.current) {
        window.clearInterval(timer);
        return;
      }
      void pollStatus();
    }, POLL_MS);
    return () => window.clearInterval(timer);
    // 仅随 fileId 重建轮询，避免 refresh 引用变化导致狂刷
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileId]);

  const chunkTypes = useMemo(() => {
    return Array.from(
      new Set(chunks.flatMap((c) => getChunkTypes(c))),
    ).sort();
  }, [chunks]);

  const filteredChunks = useMemo(() => {
    let list = chunks;
    if (typeFilter !== "all") {
      list = list.filter((c) => getChunkTypes(c).includes(typeFilter));
    }
    const q = searchText.trim().toLowerCase();
    if (q) {
      list = list.filter((c) =>
        String(c.chunk_text || "").toLowerCase().includes(q),
      );
    }
    return list;
  }, [chunks, searchText, typeFilter]);

  const progress = status?.progress ?? 0;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
        height: "100%",
        minHeight: 0,
        overflow: "auto",
      }}
    >
      <Space align="start" wrap style={{ width: "100%" }}>
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>
          返回列表
        </Button>
        <Title
          level={5}
          style={{
            margin: 0,
            flex: 1,
            minWidth: 0,
            wordBreak: "break-all",
            whiteSpace: "normal",
          }}
        >
          {filename}
        </Title>
        {status?.status && (
          <Tag color={statusColor(status.status)}>{status.status}</Tag>
        )}
      </Space>

      {error && <Alert type="error" message={error} showIcon />}

      <Card size="small" title="处理进度">
        <Progress
          percent={progress}
          status={
            status?.status === "failed"
              ? "exception"
              : status?.status === "processed"
                ? "success"
                : "active"
          }
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          阶段：{status?.current_stage || "-"} · {status?.message || ""}
        </Text>
        {status?.error_message && (
          <Alert
            style={{ marginTop: 8 }}
            type="error"
            message={status.error_message}
            showIcon
          />
        )}
      </Card>

      <Card size="small" title="解析 / 切片">
        <Descriptions size="small" column={2}>
          <Descriptions.Item label="元素数">
            {status?.partitioning?.total_elements ?? "-"}
          </Descriptions.Item>
          <Descriptions.Item label="切片数">
            {status?.chunking?.total_chunks ?? "-"}
          </Descriptions.Item>
          <Descriptions.Item label="平均切片长度">
            {status?.chunking?.avg_chunk_size ?? "-"}
          </Descriptions.Item>
          <Descriptions.Item label="向量进度">
            {status?.summarization
              ? `${status.summarization.processed_chunks}/${status.summarization.total_chunks}`
              : "-"}
          </Descriptions.Item>
        </Descriptions>
        {status?.partitioning?.element_types && (
          <Space wrap style={{ marginTop: 8 }}>
            {Object.entries(status.partitioning.element_types).map(([k, v]) =>
              v ? (
                <Tag key={k}>
                  {k}: {v}
                </Tag>
              ) : null,
            )}
          </Space>
        )}
      </Card>

      <Card size="small" title={`切片浏览器 (${chunks.length})`}>
        {chunksLoading && chunks.length === 0 ? (
          <div style={{ textAlign: "center", padding: 40 }}>
            <Spin tip="加载切片..." />
          </div>
        ) : chunks.length === 0 ? (
          <Empty description="处理中或暂无切片" />
        ) : (
          <>
            <Row gutter={[16, 12]} style={{ marginBottom: 12 }}>
              <Col xs={24} md={16}>
                <Search
                  placeholder="搜索切片内容..."
                  prefix={<SearchOutlined />}
                  value={searchText}
                  onChange={(e) => setSearchText(e.target.value)}
                  allowClear
                />
              </Col>
              <Col xs={24} md={8}>
                <Select
                  style={{ width: "100%" }}
                  value={typeFilter}
                  onChange={setTypeFilter}
                  options={[
                    { value: "all", label: "全部类型" },
                    ...chunkTypes.map((t) => ({ value: t, label: t })),
                  ]}
                />
              </Col>
            </Row>
            <Text type="secondary" style={{ display: "block", marginBottom: 12 }}>
              显示 {filteredChunks.length} / {chunks.length} 条
            </Text>
            <List
              grid={{ gutter: 16, xs: 1, sm: 1, md: 2, lg: 2, xl: 3 }}
              dataSource={filteredChunks}
              renderItem={(chunk) => {
                const types = getChunkTypes(chunk);
                const elemCount =
                  chunk.element_count ??
                  chunk.element_ids?.length ??
                  types.length;
                return (
                  <List.Item>
                    <Card
                      hoverable
                      size="small"
                      onClick={() => {
                        setSelected(chunk);
                        setDrawerOpen(true);
                      }}
                      title={
                        <Space>
                          <FileTextOutlined />
                          <span>切片 #{chunk.chunk_index}</span>
                        </Space>
                      }
                      extra={
                        <Space size={[0, 4]} wrap>
                          {chunk.is_embedded ? (
                            <Tag color="green">已向量</Tag>
                          ) : (
                            <Tag>未向量</Tag>
                          )}
                          {types.slice(0, 2).map((t) => (
                            <Tag key={t} color="blue">
                              {t}
                            </Tag>
                          ))}
                        </Space>
                      }
                    >
                      <Space direction="vertical" style={{ width: "100%" }} size={4}>
                        <Text strong>{chunk.char_count ?? 0} 字符</Text>
                        <Text type="secondary">元素约 {elemCount} 个</Text>
                        {(chunk.page_numbers || []).length > 0 && (
                          <Text type="secondary">
                            页码: {(chunk.page_numbers || []).join(", ")}
                          </Text>
                        )}
                        <Paragraph
                          ellipsis={{ rows: 3 }}
                          style={{ marginBottom: 0, color: "#666" }}
                        >
                          {highlightText(String(chunk.chunk_text || ""), searchText)}
                        </Paragraph>
                      </Space>
                    </Card>
                  </List.Item>
                );
              }}
            />
          </>
        )}
      </Card>

      <Drawer
        title={`切片 #${selected?.chunk_index ?? ""} 详情`}
        width={720}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        destroyOnClose
      >
        {selected && (
          <Space direction="vertical" size="large" style={{ width: "100%" }}>
            <Card size="small" title="元数据">
              <Space direction="vertical" style={{ width: "100%" }}>
                <Text>
                  <strong>文件：</strong>
                  {filename}
                </Text>
                <Text>
                  <strong>字符数：</strong>
                  {selected.char_count ?? 0}
                </Text>
                <Text>
                  <strong>向量：</strong>
                  {selected.is_embedded
                    ? `已写入 (${selected.vector_id || "-"})`
                    : "未写入"}
                </Text>
                {(selected.page_numbers || []).length > 0 && (
                  <Text>
                    <strong>页码：</strong>
                    {(selected.page_numbers || []).join(", ")}
                  </Text>
                )}
                <div>
                  <strong>元素 / 内容类型：</strong>
                  <div style={{ marginTop: 8 }}>
                    {getChunkTypes(selected).map((t) => (
                      <Tag key={t} color="blue">
                        {t}
                      </Tag>
                    ))}
                    {getChunkTypes(selected).length === 0 && (
                      <Text type="secondary">无</Text>
                    )}
                  </div>
                </div>
                {(selected.element_ids || []).length > 0 && (
                  <div>
                    <strong>Element IDs：</strong>
                    <div style={{ marginTop: 8, maxHeight: 120, overflow: "auto" }}>
                      {(selected.element_ids || []).map((id) => (
                        <Tag key={String(id)}>{id}</Tag>
                      ))}
                    </div>
                  </div>
                )}
              </Space>
            </Card>
            <Card size="small" title="全文">
              <Paragraph style={{ whiteSpace: "pre-wrap", fontSize: 14, marginBottom: 0 }}>
                {highlightText(String(selected.chunk_text || ""), searchText)}
              </Paragraph>
            </Card>
          </Space>
        )}
      </Drawer>
    </div>
  );
}
