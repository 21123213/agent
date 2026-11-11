import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Props = {
  content: string;
  /** 用户气泡用反色，避免链接/代码块难辨认 */
  tone?: "assistant" | "user";
};

/**
 * 对话气泡 Markdown 渲染。
 * - 默认不启用 raw HTML（无 rehype-raw），避免模型输出注入
 * - GFM：表格 / 删除线 / 任务列表等
 */
export default function ChatMarkdown({ content, tone = "assistant" }: Props) {
  const text = (content || "").trim();
  if (!text) return null;

  return (
    <div className={`chat-md chat-md--${tone}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
