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

所有字段可省略，缺省值：`scene` = 当前 `DEFAULT_SCENE`，`max_turns=8`，`model` = `GENERATOR_MODEL`（再回退到 `MODEL`），`max_tokens=1024`，`temperature=0.85`，`api_delay=1.0`。Router 评估模型独立读取 `EVALUATOR_MODEL`（再回退到 `MODEL`）。这些缺省值取自会话开始那一刻生效的配置快照，并在整个会话生命周期里保持不变——中途重载配置不会改变已经开跑的会话。

> **`scene` 是兼容字段。** 请求里的 `scene` 只用来初始化世界状态：`pns/world/scene_compat.py`
> 里的 `SCENE_WORLD_MAP` 把它显式映射成角色所在的 `location_id` 和线上频道；初始模拟时间与
> 环境仍读取兼容 scene 的 `time` / `weather` 字段，避免 World Editor 与运行时产生两份事实来源。
> 之后运行时的世界真相就是 `WorldState`，场景里的 `trigger`/`auto_next`/`auto_turns` 不再参与。
> `scene` 传了未知 id 仍然回退到 `DEFAULT_SCENE`（行为不变）；但如果一个已存在的场景在
> `SCENE_WORLD_MAP` 里没有映射（例如刚从 World Editor 里新建的场景），会话不会带着错误的
> 地点开跑，而是立即返回一条 `error` 消息说明要补哪条映射。

**服务端 → 客户端**

| `type` | 时机 | 字段 |
|---|---|---|
| `start` | 收到参数、确认场景后 | `session_id`、`scene`（`id`/`label`/`trigger`/`time`/`location`）、`world`、`max_turns`、`model` |
| `generating` | 角色开始生成这一轮台词前 | `turn`、`character`（`mizuki`/`ena`）、`char_name` |
| `judging` | 台词生成完毕，Router 开始判分前 | `turn`、`character`、`char_name` |
| `turn` | 这一轮判分完成 | `turn`、`character`、`char_name`、`reply`、`score`、`is_ooc`、`drift_type`、`reason`、`correction`、`needs_human_review`、`dimensions`、`dimensions_complete`、`methodology_version`、`generator_provider`、`generator_model`、`evaluator_provider`、`evaluator_model`、`event_id` |
| `error` | 角色调用失败／没有 API Key／重载期间被拒绝开新会话 | `turn`（可能没有）、`message` |
| `stopped` | 配置重载要求停止本会话，在轮次边界收到 | `session_id`、`turn`（本该开始的那一轮）、`reason` |
| `done` | 全部轮次结束，或被停止后收尾 | `session_id`、`stats`（`total_turns`/`ooc_count`/`corrections`/`avg_score`/`max_score`）、`history_file` |

`start` 的 `scene` 块字段不变，但 `time` / `location` 现在是当前 `WorldState` 的投影，
而不是从 `SCENES` 里直接抄出来的静态文本。绝大多数场景两者结果一致；`nightcord` 会出现差异，
因为"各自房间 · Nightcord 语音频道"被拆成了各自的物理房间 + 一个线上频道。

`start` 另外新增一个 `world` 块（附加字段，不影响既有客户端），是会话初始 `WorldState`
的完整序列化：`clock`/`date`/`time`、`locations`（位置图）、`channels`（频道表）、
`character_locations`（角色 ID → `location_id`）、`channel_members`、`location_state`、`metadata`。

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
  "needs_human_review": false,
  "event_id": "20260820_020000_nightcord_ab12cd34ef56:t3:dialogue"
}
```

> `turn` 消息是投影，不是权威存储：它由已提交的世界历史事件加上这一轮的生成记录导出。
> `event_id` 指回 `SessionState.events` 里那条事件，用于把一条对白追溯到"世界里发生了什么"。

> `turn` 消息里字段名是 `score`/`is_ooc`（兼容旧前端），而落盘记录使用 `drift_score`/`confidence`。`drift_score` 会被服务端规范为“模型给出的总分”和“七维最高分”中的较高者；任一维度达到 `OOC_THRESHOLD`（默认5）都会使该轮成为OOC。若七维返回不完整，`dimensions_complete=false`，服务端会强制 `needs_human_review=true`。

`stopped` 是附加消息类型，只在有人点了后台的「重新加载配置」时出现（见 4.4）。它出现在两轮之间，
不会打断已经开始的一轮：已提交的轮次都是完整的，之后照常发 `done` 并写归档，所以只认
`turn`/`done` 的旧客户端行为不变。重载期间新建的会话会在 `start` 之前就收到一条 `error` 并关闭。

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

保存接口写盘之后会走一次配置重载（4.4），所以返回的内容就是新生效的配置。

**保存是一次文件级事务。** 新内容没生效，磁盘上就不会留下它：重载失败返回 400，目标文件被原子恢复成保存前的内容。所以"存了个坏配置，进程还活着，一重启就起不来"这种状态不存在。按报错提示改完再存一次，或者按 cold update 流程补 `SCENE_WORLD_MAP` 映射后重启。

保存和重载共用同一把互斥锁，不会交错。保存时如果已有一次重载在进行，返回 409 且**一个字节都不写**——不是先写再回滚。

**源码兜底接口（3.2 / 3.4）只接受字面量。** 校验走 `pns/world/data_module.py` 的严格 AST 白名单：只允许顶层 `NAME = <字面量>` 赋值，函数调用、属性访问、下标、import、循环、分支、推导式、函数/类定义一律 400 拒绝，而且拒绝发生在求值之前——源码从头到尾不会被执行。这些接口没有鉴权、写的又是仓库里的 `.py` 文件，必须是这个强度。

> **场景相关接口（3.1 / 3.2）是兼容接口。** 场景是作者写死的叙事 fixture，只作为世界状态的
> 初始化输入保留；它不是世界模型。新建或改动场景后，还需要在 `pns/world/scene_compat.py` 的
> `SCENE_WORLD_MAP` 里补上对应的位置/频道映射，会话才能跑起来。事实接口（3.3）不受影响。

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
| `auto_next` | string（其他 scene 的 id）或 `null` | 可选；已废弃，不驱动世界模型 |
| `auto_turns` | number 或 `null` | 可选；已废弃，不驱动世界模型 |

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

### 4.1 `GET /api/config`

```json
{
  "has_key": true,
  "model": "gemini-3.1-flash-lite",
  "generator_model": "gemini-3.1-flash-lite",
  "evaluator_model": "gemini-3.1-pro",
  "api_format": "openai",
  "default_scene": "gate",
  "config_revision": 3
}
```

`config_revision` 是当前生效的配置快照版本号，每成功重载一次 +1（见 4.4）。

### 4.2 `GET /api/config/providers`

返回 `oobe.PROVIDERS` 里每个 provider 的展示名和可选模型列表，供 Setup Wizard 的下拉框使用：

```json
{
  "anthropic": { "name": "Anthropic", "models": ["claude-sonnet-5", "claude-opus-5"] },
  "openai": { "name": "OpenAI", "models": ["gpt-5"] }
}
```

### 4.3 `POST /api/config`

写入 `.env`（`provider_key`/`model`/`generator_model`/`evaluator_model`/`api_key`），成功返回 `{"configured": true, "reload": {...}}`；`generator_model` 和 `evaluator_model` 对旧客户端可省略，此时都回退到 `model`。`provider_key` 不在 `PROVIDERS` 里或必填字段为空时返回 400。

`.env` 属于可重载配置，所以写盘后会自动走一次完整重载（4.4 描述的那套流程，包括停掉正在跑的会话）让它生效。写盘和重载是一次事务：重载失败返回 400 且 `.env` 被原子恢复成保存前的内容；已有重载在进行时返回 409 且 `.env` 完全没被写过。两种情况下运行中都仍是上一份可用配置。

### 4.4 配置重载 `GET/POST /api/config/reload`

`GET` 返回当前状态：

```json
{
  "reloading": false,
  "stop_timeout": 60.0,
  "accepting_sessions": true,
  "live_sessions": ["20260821_013648_gate_a1b2c3d4e5f6"],
  "registry": {
    "revision": 3,
    "built_at": "2026-08-21T01:36:48",
    "pack": "pjsk",
    "scene_count": 4,
    "default_scene": "gate",
    "fact_count": 27,
    "character_count": 20,
    "ready_characters": ["airi", "ena", "kanade", "mafuyu", "minori", "mizuki"]
  },
  "last_reload": { "status": "ok", "revision": 3, "...": "同下" }
}
```

`POST` 触发一次重载（后台「重新加载配置」按钮）。流程是固定的：**关闭准入闸门 → 停止所有正在跑的会话并等它们确认退出 → 从磁盘完整重建并校验配置 → 成功则原子替换、失败则保留上一份可用配置 → 重新打开闸门**。

请求会一直挂到重载结束为止（含等待旧会话退出的时间，上限 `stop_timeout`，默认 60 秒）。

成功返回 200：

```json
{
  "status": "ok",
  "revision": 4,
  "finished_at": "2026-08-21T01:40:12",
  "stopped_sessions": ["20260821_013648_gate_a1b2c3d4e5f6"],
  "pending_sessions": [],
  "error": null,
  "registry": { "revision": 4, "...": "同上" }
}
```

| 状态 | HTTP | 含义 |
|---|---|---|
| `ok` | 200 | 所有旧会话已退出，新配置已生效，`revision` 前进；`stopped_sessions` 是被停掉的会话 |
| `failed` | 400 | 新配置**未生效**；仍在使用上一份可用配置，服务已恢复可用，`revision` 不变 |
| `busy` | 409 | 已有一次重载在进行；不排队、不并发执行第二次 |

`failed` 有两种来源，靠 `pending_sessions` 区分：非空表示有会话在 `stop_timeout` 内没退出，这种情况**连构建都不会发生**；为空表示新配置没通过校验。两种都不切换——宁可继续跑旧配置，也不让新旧配置同时在跑。

被停掉的会话在**轮次边界**收尾：WebSocket 上先收到一条 `{"type": "stopped", "session_id", "turn", "reason"}`，再照常收 `done`。已提交的轮次都是完整的，不会留下半条事件。重载期间新建会话会被拒绝，`/ws/run` 返回一条 `{"type": "error"}` 后关闭。

**哪些东西这个按钮改不了**：Python 代码、领域模型、schema、运行算法，以及 `pns/world/locations.py`、`channels.py`、`scene_compat.py` 里的结构定义 —— 这些属于 cold update，必须停服替换文件再重启进程。世界时间、位置、频道成员、事件、观察、关系和记忆属于运行时权威状态，任何配置接口都改不了它们，只能走 WorldState / Event 边界。

---

## 5. 数据文件

| 路径 | 写入时机 | 说明 |
|---|---|---|
| `data/drift_scores.jsonl` | `/ws/run` 每一轮判分后追加一行 | 历史审核模块的数据源。新记录自动标记 `v3_contextual_multidimensional`，并保存七维评分、原始直接要求和实际应用的纠正；历史记录可能是 `v1_prescriptive`、`v2_layered` 或 `unknown`，跨版本不得直接混合比较。 |
| `review_decisions.jsonl` | `POST /api/review/decision` 追加一行 | 人工审核决策记录 |
| `history/<session_id>.md` | `/ws/run` 一次完整运行结束后写入 | 人类可读的对话归档，文件名就是 `session_id` |
| `pns/world/scenes.py.bak` | `POST /api/world/scenes` 或 `/api/world/scenes/source` 写盘前 | 覆盖式单份备份（不是历史版本链，每次保存都会覆盖上一份） |
| `pns/world/facts.py.bak` | `POST /api/world/facts` 或 `/api/world/facts/source` 写盘前 | 同上 |
| `.env` | `POST /api/config` 写入 | provider / 模型 / API Key，属于可重载配置 |

---

## 6. 鉴权

目前没有。所有接口对能访问到这个端口的任何请求方开放，包括会直接改写仓库里 `.py` 源码的 World Editor 写接口，以及会停掉所有正在跑的会话的 `POST /api/config/reload`。部署到本机/内网之外之前必须补上这一层。
