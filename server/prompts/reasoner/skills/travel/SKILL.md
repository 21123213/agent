---
name: travel-planning
description: |
  旅游行程规划技能。当用户提到出行、旅游、行程、攻略、玩法、景点、酒店、
  天气、闭馆、节假日活动，或明确要规划某地几日游时启用。
version: 1.0.0
---

# 旅游规划 Skill

你在处理**旅游/行程**相关需求。请优先用下方**已注册的旅游工具**完成，而不是凭空硬猜。

> 玩法素材以 `web_search` 为主（核心工具，始终可用）。  
> **无小红书搜索**；不要调用不存在的 `xhs_search` / `search_play`。

## 先理解用户再说（必守）

从自然语言里提取意图，**不要按英文字段清单硬套「缺参」**：

- 「杭州」「本周周二到周五」「四天三晚」等 → 已具备目的地与时段，应直接规划工具，**禁止**再问「去哪个城市」「哪天出发」。
- 相对日期（本周/下周/这周二～周五）→ 计划里用 `get_time` 锚定「今天」，再推算具体 `YYYY-MM-DD`；**不要**为此向用户要绝对日期。
- 同行人、预算、住宿档、玩法偏好：**未提就用合理默认**（舒适档、经典不过度特种兵节奏）。
- **仅当真正无法开工**时才短问一句（例如完全没说去哪、也没说大致方向）。

## 对用户回答（必守）

最终回复是给**普通旅客**看的：

- **禁止**出现工具名、字段名或内部术语（如 `search_stay`、`price_per_night`、`lat`/`lng`、JSON）。
- 用白话转述；无实时房价时说「暂无实时房价」，**不要编造价格**。
- 档位若需提及，写「豪华 / 舒适 / 经济」。

## 始终可用（core）

| 工具 | 用途 |
|------|------|
| `web_search` | **主路径**：博查网页搜索；三日游路线、季节玩法、预约票务等 |
| `get_time` | 取当前时间；相对日期场景的第一步 |
| `calculator` | 天数/预算粗算 |

## 旅游专用工具

| 工具 | 何时用 |
|------|--------|
| `get_culture` | 闭馆/开放时间与当地活动**原始检索**；须带 `start_date`，可选 `season` |
| `get_weather` | 多日天气与是否适合户外 |
| `geocode_places` | 地名批量转经纬度（可选；`build_route` 内部也会 geocode） |
| `build_route` | 把你整理好的线路/散落点编成每日行程 |
| `search_stay` | 按日锚点搜酒店；用户问住宿/酒店时必用 |
| `plan_transport` | 市内接驳；`hotel` 传 `search_stay` 返回的酒店对象 |

## 推荐顺序（PE 用 depends_on 分层）

1. **时间锚点（相对日期时）**：`get_time`。
2. **玩法素材**：`web_search`（可并行多条 query）。
3. **约束**：`get_weather` + `get_culture`（`start_date` 用推算出的具体日期）。
4. 整理 `itinerary_lines` / `loose_pois` → `build_route`。
5. 需要住宿/交通：`search_stay` → `plan_transport`（写 depends_on）。

## `build_route` 入参约定

`itinerary_lines` 每项必须是：

```json
{
  "id": "line_1",
  "title": "西湖半日",
  "stops": [
    {"name": "断桥残雪", "role": "poi"},
    {"name": "白堤", "role": "poi"}
  ]
}
```

- **必须用 `stops`**，不要用 `points` / `pois`。
- `loose_pois` 只放不成线的点。

## 住宿与交通

- `search_stay`：尽量带上 `build_route` 的日程；档位默认舒适，不要追问英文参数名。
- 无报价字段 → 对用户只说「暂无实时房价」。
- `plan_transport` 的 `hotel` 传 `search_stay` 的 hotel 对象（含坐标）；缺坐标时系统会用行程首站顶上。

## 硬性约定

- **禁止改写**用户给定的目的地、日期与核心需求。
- `get_culture` 是检索线索，不是已过滤结论；不要编造未出现的闭馆日、票价、活动档期。
