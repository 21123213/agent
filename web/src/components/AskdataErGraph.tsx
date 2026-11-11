/**
 * 智能问数业务表关系图（订单中心精简版）
 * 纯 SVG，无额外依赖；与 docs/askdata-er.md / 003_biz_askdata.sql 对齐。
 */
import { Typography } from "antd";

const { Text } = Typography;

type NodeDef = {
  id: string;
  label: string;
  x: number;
  y: number;
  kind?: "core" | "dim" | "view" | "sys";
};

type EdgeDef = { from: string; to: string; label?: string };

const W = 720;
const H = 360;
const NW = 118;
const NH = 36;

const NODES: NodeDef[] = [
  { id: "s_unit", label: "s_unit 租户", x: 40, y: 150, kind: "sys" },
  { id: "biz_channel", label: "biz_channel", x: 220, y: 40, kind: "dim" },
  { id: "biz_store", label: "biz_store 门店", x: 220, y: 110, kind: "dim" },
  { id: "biz_customer", label: "biz_customer", x: 220, y: 200, kind: "dim" },
  { id: "biz_category", label: "biz_category", x: 220, y: 280, kind: "dim" },
  { id: "biz_product", label: "biz_product", x: 400, y: 280, kind: "dim" },
  { id: "biz_sales_order", label: "biz_sales_order", x: 400, y: 150, kind: "core" },
  { id: "biz_sales_order_item", label: "order_item", x: 580, y: 220, kind: "core" },
  { id: "biz_payment", label: "biz_payment", x: 580, y: 80, kind: "core" },
  { id: "biz_refund", label: "biz_refund", x: 580, y: 150, kind: "core" },
  { id: "biz_daily_sales_agg", label: "daily_sales_agg VIEW", x: 400, y: 40, kind: "view" },
];

const EDGES: EdgeDef[] = [
  { from: "biz_store", to: "s_unit" },
  { from: "biz_customer", to: "s_unit" },
  { from: "biz_sales_order", to: "s_unit" },
  { from: "biz_payment", to: "s_unit" },
  { from: "biz_refund", to: "s_unit" },
  { from: "biz_store", to: "biz_channel" },
  { from: "biz_sales_order", to: "biz_channel" },
  { from: "biz_sales_order", to: "biz_store" },
  { from: "biz_sales_order", to: "biz_customer" },
  { from: "biz_sales_order_item", to: "biz_sales_order" },
  { from: "biz_sales_order_item", to: "biz_product" },
  { from: "biz_product", to: "biz_category" },
  { from: "biz_payment", to: "biz_sales_order" },
  { from: "biz_refund", to: "biz_sales_order" },
  { from: "biz_daily_sales_agg", to: "biz_sales_order", label: "聚合" },
];

function fill(kind?: NodeDef["kind"]) {
  switch (kind) {
    case "core":
      return "#d9f2ec";
    case "view":
      return "#fff7e6";
    case "sys":
      return "#f0f0f0";
    default:
      return "#eef3f8";
  }
}

function stroke(kind?: NodeDef["kind"]) {
  switch (kind) {
    case "core":
      return "#1a7f64";
    case "view":
      return "#d48806";
    case "sys":
      return "#8c8c8c";
    default:
      return "#5b7c99";
  }
}

function center(n: NodeDef) {
  return { x: n.x + NW / 2, y: n.y + NH / 2 };
}

export default function AskdataErGraph() {
  const byId = Object.fromEntries(NODES.map((n) => [n.id, n]));

  return (
    <div
      style={{
        border: "1px solid #e8ecea",
        borderRadius: 8,
        padding: "10px 12px 8px",
        background: "#fafcfb",
        marginBottom: 12,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          marginBottom: 6,
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <Text strong style={{ fontSize: 13 }}>
          业务表关系（订单中心）
        </Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          箭头：从表 → 父表（外键）· 绿=交易 · 黄=日汇总视图
        </Text>
      </div>
      <div style={{ width: "100%", overflowX: "auto" }}>
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          style={{ minWidth: 560, maxHeight: 320, display: "block" }}
          role="img"
          aria-label="AskData 业务表 ER 图"
        >
          <defs>
            <marker
              id="askdata-er-arrow"
              markerWidth="8"
              markerHeight="8"
              refX="7"
              refY="3"
              orient="auto"
            >
              <path d="M0,0 L7,3 L0,6 Z" fill="#8aa0b0" />
            </marker>
          </defs>
          {EDGES.map((e, i) => {
            const a = byId[e.from];
            const b = byId[e.to];
            if (!a || !b) return null;
            const p1 = center(a);
            const p2 = center(b);
            const mx = (p1.x + p2.x) / 2;
            const my = (p1.y + p2.y) / 2;
            return (
              <g key={`${e.from}-${e.to}-${i}`}>
                <line
                  x1={p1.x}
                  y1={p1.y}
                  x2={p2.x}
                  y2={p2.y}
                  stroke="#b7c5cf"
                  strokeWidth={1.2}
                  markerEnd="url(#askdata-er-arrow)"
                />
                {e.label ? (
                  <text
                    x={mx}
                    y={my - 4}
                    textAnchor="middle"
                    fontSize={10}
                    fill="#8c8c8c"
                  >
                    {e.label}
                  </text>
                ) : null}
              </g>
            );
          })}
          {NODES.map((n) => (
            <g key={n.id}>
              <rect
                x={n.x}
                y={n.y}
                width={NW}
                height={NH}
                rx={8}
                fill={fill(n.kind)}
                stroke={stroke(n.kind)}
                strokeWidth={1.4}
              />
              <text
                x={n.x + NW / 2}
                y={n.y + NH / 2 + 4}
                textAnchor="middle"
                fontSize={11}
                fill="#1f2a32"
                fontFamily="ui-sans-serif, system-ui, sans-serif"
              >
                {n.label}
              </text>
            </g>
          ))}
        </svg>
      </div>
      <Text type="secondary" style={{ fontSize: 12 }}>
        可问：今日/本月销售额、按门店汇总、退款金额按品牌汇总。品牌在{" "}
        <code>biz_product.brand_name</code>。
      </Text>
    </div>
  );
}
