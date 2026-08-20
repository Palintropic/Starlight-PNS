# PNS Dashboard API

`scripts/server.py` 是 PNS 的唯一服务入口，提供本文档列出的所有 HTTP/WebSocket 接口。目前**没有任何鉴权机制**——服务假定运行在本地或受信任网络里，供单个使用者操作；如果之后要对外暴露，需要在这层之上补鉴权。

所有 JSON 请求/响应均为 `application/json`，字符编码 UTF-8。

---

## 1. 实时对话生成

### `WS /ws/run`

建立连接后，客户端先发送一条 JSON 作为运行参数，服务端随即开始多轮对话循环，每个阶段推送一条消息。

**客户端 → 服务端（连接建立后立即发送一次）**

```json
{
  "scene": "gate",
  "max_turns": 8,
  "model": "gemini-3.1-flash-lite",
  "max_tokens": 1024,
  "temperature": 0.85,
  "api_delay": 1.0
}
```

所有字段可省略，缺省值：`scene` = 当前 `DEFAULT_SCENE`，`max_turns=8`，`model` = 环境变量 `GENERATOR_MODEL`（再回退到 `MODEL`），`max_tokens=1024`，`temperature=0.85`，`api_delay=1.0`。Router 评估模型独立读取 `EVALUATOR_MODEL`（再回退到 `MODEL`）。

**服务端 → 客户端**

| `type` | 时机 | 字段 |
|---|---|---|
| `start` | 收到参数、确认场景后 | `session_id`、`scene`（`id`/`label`/`trigger`/`time`/`location`）、`max_turns`、`model` |
| `generating` | 角色开始生成这一轮台词前 | `turn`、`character`（`mizuki`/`ena`）、`char_name` |
| `judging` | 台词生成完毕，Router 开始判分前 | `turn`、`character`、`char_name` |
| `turn` | 这一轮判分完成 | `turn`、`character`、`char_name`、`reply`、`score`、`is_ooc`、`drift_type`、`reason`、`correction`、`needs_human_review`、`dimensions`、`dimensions_complete`、`methodology_version`、`generator_provider`、`generator_model`、`evaluator_provider`、`evaluator_model` |
| `error` | 角色调用失败／没有 API Key | `turn`（可能没有）、`message` |
| `done` | 全部轮次结束 | `session_id`、`stats`（`total_turns`/`ooc_count`/`corrections`/`avg_score`/`max_score`）、`history_file` |

示例（`turn` 消息）：

```json
{
  "type": "turn",
  "turn": 3,
  "character": "ena",
  "char_name": "绘名",
  "reply": "……嗯，是挺好看的。",
  "score": 2,
  "is_ooc": false,
  "drift_type": "无",
  "reason": "省略号收尾+简短回应，与瑞希互动时的克制符合设定。",
  "dimensions": {
    "character_facts": {"score": 0, "reason": "未出现事实冲突。"},
    "psychological_mechanism": {"score": 1, "reason": "符合关系语境。"},
    "language_structure": {"score": 1, "reason": "长度与停顿自然。"},
    "media_authenticity": {"score": 1, "reason": "像即时对话。"},
    "task_compliance": {"score": 0, "reason": "遵守只输出台词的要求。"},
    "unsupported_invention": {"score": 0, "reason": "没有补写具体事实。"},
    "timeline_boundary": {"score": 0, "reason": "未越过时间线。"}
  },
  "dimensions_complete": true,
  "methodology_version": "v3_contextual_multidimensional",
  "correction": null,
  "needs_human_review": false
}
```

> `turn` 消息里字段名是 `score`/`is_ooc`（兼容旧前端），而落盘记录使用 `drift_score`/`confidence`。`drift_score` 会被服务端规范为“模型给出的总分”和“七维最高分”中的较高者；任一维度达到 `OOC_THRESHOLD`（默认5）都会使该轮成为OOC。若七维返回不完整，`dimensions_complete=false`，服务端会强制 `needs_human_review=true`。

`session_id` 由时间、场景 ID 和随机唯一后缀组成；同一次运行里 markdown 归档（`history/<session_id>.md`）和 `data/drift_scores.jsonl` 里的记录共用这个 ID，方便互相对照。

---

## 2. 历史审核

### `GET /api/review/turns`

逐行读取 `data/drift_scores.jsonl` 并原样返回列表；文件不存在时返回 `[]`。

```json
[
  {
    "session_id": "20260802_170000_gate",
    "turn": 1,
    "character": "mizuki",
    "char_name": "瑞希",
    "text": "绘名绘名！今天的天空颜色超好看的喵！",
    "drift_score": 1,
    "confidence": 0.92,
    "drift_type": "无",
    "reason": "语气活泼、话题跳跃，符合瑞希日常状态。",
    "needs_human_review": false,
    "correction": null,
    "dimensions": {
      "character_facts": {"score": 0, "reason": "未出现事实冲突。"},
      "psychological_mechanism": {"score": 1, "reason": "符合角色机制。"}
    },
    "dimensions_complete": true,
    "methodology_version": "v3_contextual_multidimensional",
    "original_request": "绘名：要不要晚点碰面？",
    "correction_applied": null,
    "timestamp": "2026-08-02T17:00:12.345678"
  }
]
```

### `GET /api/review/decisions`

返回人工决策记录，key 是 `"{session_id}:{turn}"`。

```json
{
  "20260802_170000_gate:2": {
    "session_id": "20260802_170000_gate",
    "turn": 2,
    "character": "mizuki",
    "decision": "rewrite",
    "note": "去掉征询式收尾",
    "decided_at": "2026-08-02T17:05:00.000000"
  }
}
```

### `POST /api/review/decision`

```json
// request
{ "session_id": "20260802_170000_gate", "turn": 2, "character": "mizuki", "decision": "rewrite", "note": "去掉征询式收尾" }
```

`decision` 只能是 `approve` / `reject` / `rewrite`。响应是写入的完整记录（附带服务端生成的 `decided_at`）。追加写入 `review_decisions.jsonl`。

---

## 3. World Editor

图形化编辑 `pns/world/scenes.py` / `pns/world/facts.py`。写回逻辑在 `pns/world/codegen.py`：只替换目标变量（`SCENES`/`WORLD_FACTS`）的赋值范围，文件头注释和其他顶层语句原样保留；写盘前用 `black` 格式化，写盘前把原文件备份成同目录下的 `*.py.bak`（见第 4 节）；`POST` 成功后服务端会 `importlib.reload` 相关模块，正在运行的进程无需重启即可读到新内容。

校验失败（语法错误、缺变量、类型不对、`id` 与 dict key 不一致等）一律返回 **HTTP 400**，body 形如 `{"detail": "错误信息"}`，不会写盘。

### 3.1 场景 `GET/POST /api/world/scenes`

**`GET`** 返回完整 `SCENES` dict。实时对话模块（module①）的场景下拉框也复用这一个接口——两边合并进同一个 dashboard 前端后，不再需要单独维护一份精简版。

```json
{
  "gate": {
    "id": "gate",
    "label": "神山高校校门口",
    "time": "傍晚 17:30",
    "location": "神山高校校门口",
    "weather": "晴，微风",
    "day_phase": "evening",
    "scene_type": "area_talk",
    "lore_tag": "软推断",
    "trigger": "瑞希放学往外走，绘名刚到校门口准备进去——两人正面碰上。",
    "gate_triggers": { "A": "...", "B": "...", "C": "..." },
    "gate_opening_note": "当前脚本opening台词标记为trigger_B待修...",
    "auto_next": "nightcord",
    "auto_turns": 8
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 必须和外层 dict 的 key 一致，否则 400 |
| `label`/`time`/`location`/`weather`/`trigger` | string | 自由文本 |
| `day_phase` | `"morning"\|"afternoon"\|"evening"\|"late_night"` | 枚举 |
| `lore_tag` | `"硬事实"\|"软推断"\|"待验证"` | 枚举 |
| `scene_type` | string | 目前只有 `area_talk`，留作可扩展 |
| `gate_triggers` | `{A,B,C: string}` 或 `null` | 可选，`null`/缺失时整个 key 不写回源码 |
| `gate_opening_note` | string 或 `null` | 可选，同上 |
| `auto_next` | string（其他 scene 的 id）或 `null` | 可选 |
| `auto_turns` | number 或 `null` | 可选 |

**`POST`** body 是编辑后的完整 dict（同上结构，key 为 scene id），响应是写盘后重新读取的 `SCENES`。

### 3.2 场景源码兜底 `GET/POST /api/world/scenes/source`

```json
{ "source": "# pns/world/scenes.py\n# 从 world.py v0.6 迁移\nSCENES = {\n    \"gate\": {...}\n}\n\nDEFAULT_SCENE = \"gate\"\n" }
```

`POST` body 同形状（`{"source": "..."}`）。服务端会 `ast.parse` + 在禁用 `__builtins__` 的命名空间里 `exec`，确认顶层存在 `SCENES` 且是 dict，再过 `black` 格式化、备份、写盘。校验失败返回 400，不写盘。

### 3.3 世界设定 `GET/POST /api/world/facts`

**`GET`**：

```json
{
  "facts": {
    "school": "神山高校（全日制+夜间定时制并存）",
    "mizuki_gender_note": "⚠ 暁山瑞希的性别认同...",
    "25ji_shinonome_ena": "东云绘名；..."
  },
  "groups": {
    "学校/作息": ["school", "ena_schedule", "mizuki_schedule", "intersection_daytime", "intersection_night", "class_2b_classmate"],
    "性别认同注记": ["mizuki_gender_note"],
    "25ji": ["25ji_shinonome_ena", "25ji_akiyama_mizuki", "25ji_hiiragi_kanade", "25ji_asahi_mafuyu"],
    "VBS": ["vbs_shinonome_akito", "vbs_shiraishi_an", "vbs_aoyagi_toya", "vbs_azusawa_kohane"],
    "WxS": ["wxs_tenma_tsukasa", "wxs_otori_emu", "wxs_kusanagi_nene", "wxs_kamishiro_rui"],
    "MMJ": ["mmj_hanasato_minori", "mmj_kiritani_haruka", "mmj_momoi_airi", "mmj_hinomori_shizuku"],
    "Leo/need": ["leoneed_hoshino_ichika", "leoneed_tenma_saki", "leoneed_mochizuki_honami", "leoneed_hinomori_shiho"]
  }
}
```

`groups` 是 `pns/world/codegen.py` 里 `FACT_GROUPS` 的原样输出——写回 `facts.py` 时就是按这份映射重新生成 `# ─── 分组名 ───` 注释的，不在映射里的 key 会被放进源码里的 `# ─── 未分组 ───` 区。这份映射只在后端维护一份，前端不需要（也不应该）自己再定义一套分组。

**`POST`** body：

```json
{ "facts": { "school": "...", "test_new_key": "新增的一条设定" } }
```

`facts` 是扁平的 `key -> string`。响应形状与 `GET` 相同（写盘后重新读取的 `facts`/`groups`）。新增的、`FACT_GROUPS` 里没有的 key 会落进"未分组"区——分组只由后端代码维护，不是每次保存都能通过前端指定。

### 3.4 世界设定源码兜底 `GET/POST /api/world/facts/source`

形状与 3.2 完全一致，只是操作对象是 `pns/world/facts.py` 里的 `WORLD_FACTS`。

---

## 4. 其余已有接口

### `GET /api/config`

```json
{
  "has_key": true,
  "model": "gemini-3.1-flash-lite",
  "generator_model": "gemini-3.1-flash-lite",
  "evaluator_model": "gemini-3.1-pro",
  "api_format": "openai",
  "default_scene": "gate"
}
```

### `GET /api/config/providers`

返回 `oobe.PROVIDERS` 里每个 provider 的展示名和可选模型列表，供 Setup Wizard 的下拉框使用：

```json
{
  "anthropic": { "name": "Anthropic", "models": ["claude-sonnet-5", "claude-opus-5"] },
  "openai": { "name": "OpenAI", "models": ["gpt-5"] }
}
```

### `POST /api/config`

写入 `.env`（`provider_key`/`model`/`generator_model`/`evaluator_model`/`api_key`），成功返回 `{"configured": true}`；`generator_model` 和 `evaluator_model` 对旧客户端可省略，此时都回退到 `model`。`provider_key` 不在 `PROVIDERS` 里或必填字段为空时返回 400。

---

## 5. 数据文件

| 路径 | 写入时机 | 说明 |
|---|---|---|
| `data/drift_scores.jsonl` | `/ws/run` 每一轮判分后追加一行 | 历史审核模块的数据源。新记录自动标记 `v3_contextual_multidimensional`，并保存七维评分、原始直接要求和实际应用的纠正；历史记录可能是 `v1_prescriptive`、`v2_layered` 或 `unknown`，跨版本不得直接混合比较。 |
| `review_decisions.jsonl` | `POST /api/review/decision` 追加一行 | 人工审核决策记录 |
| `history/<session_id>.md` | `/ws/run` 一次完整运行结束后写入 | 人类可读的对话归档，文件名就是 `session_id` |
| `pns/world/scenes.py.bak` | `POST /api/world/scenes` 或 `/api/world/scenes/source` 写盘前 | 覆盖式单份备份（不是历史版本链，每次保存都会覆盖上一份） |
| `pns/world/facts.py.bak` | `POST /api/world/facts` 或 `/api/world/facts/source` 写盘前 | 同上 |

---

## 6. 鉴权

目前没有。所有接口对能访问到这个端口的任何请求方开放，包括会直接改写仓库里 `.py` 源码的 World Editor 写接口。部署到本机/内网之外之前必须补上这一层。
