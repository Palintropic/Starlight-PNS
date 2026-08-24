# PNS Dashboard API

`scripts/server.py` 是 PNS 的唯一服务入口，提供本文档列出的所有 HTTP/WebSocket 接口。

**鉴权与授权见第 6 节。** 简单说：默认拒绝——除了一份显式的公开清单（健康检查、登录三条、
前端静态资源），其余每一条路径（含 `/ws/run`）都要求一个已认证主体；而"能做什么"由角色的
scope 决定，非安全方法默认要求 `operate`。既没配管理凭据、账户库里也没有账户的本地开发
服务器行为不变（不鉴权）；生产模式下缺凭据或缺管理员的进程根本起不来。

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

## 4.1 持久世界角色活动

### `POST /api/persistent-worlds/{world_id}/activity`

请求：

```json
{"character_id": "mizuki", "activity": "editing_video"}
```

`activity` 是服务器声明的闭集：`unspecified`、`idle`、`resting`、`studying`、
`working_part_time`、`drawing`、`composing`、`editing_video`、`online_chatting`。
接口不接受自由文本。

一次成功的新变化会提交 `character.activity_changed` 事件并立即 checkpoint，返回
`changed=true` 与 `event_id`。同值重试是幂等成功：不产生第二条事件，返回
`changed=false`；若上一请求已提交事件但 checkpoint 失败，这次重试会补完保存。
未知角色或不成立的状态转换返回 409，未知活动值由请求校验返回 422。

这条接口只改运行时权威状态，不改角色包或场景文件。生成与 Router 只会看到
当前行动角色自己的活动。

角色包里写了 `daily_rhythm` 的角色，还会在模拟时间推进时被作息表对齐到当前时段
（见 ARCHITECTURE 的 Authored daily rhythm）。操作者在一个时段之内做的改动不会
被当次推进覆盖掉：作息表要等下一段开始才重新接手。世界状态投影里
`autonomy.rhythm_characters` 列出这个世界由作息表管着的角色。

---

## 5. 数据文件

| 路径 | 写入时机 | 说明 |
|---|---|---|
| `data/drift_scores.jsonl` | `/ws/run` 每一轮判分后追加一行 | 历史审核模块的数据源。新记录自动标记 `v3_contextual_multidimensional`，并保存七维评分、原始直接要求和实际应用的纠正；历史记录可能是 `v1_prescriptive`、`v2_layered` 或 `unknown`，跨版本不得直接混合比较。 |
| `data/review_decisions.jsonl` | `POST /api/review/decision` 追加一行 | 人工审核决策记录。DEPLOY-1 之前在仓库根上，之后跟着 `data/` 走——它是运行时数据，容器部署时 `data/` 是一个卷。旧文件需要手动搬一次。 |
| `history/<session_id>.md` | `/ws/run` 一次完整运行结束后写入 | 人类可读的对话归档，文件名就是 `session_id` |
| `pns/world/scenes.py.bak` | `POST /api/world/scenes` 或 `/api/world/scenes/source` 写盘前 | 覆盖式单份备份（不是历史版本链，每次保存都会覆盖上一份） |
| `pns/world/facts.py.bak` | `POST /api/world/facts` 或 `/api/world/facts/source` 写盘前 | 同上 |
| `.env` | `POST /api/config` 写入 | provider / 模型 / API Key，属于可重载配置 |

---

## 6. 鉴权

### 6.1 默认拒绝

守卫是包在整个应用外面的 ASGI 中间件，不是挂在某几条路由上的依赖。它在路由匹配、请求体解析、
依赖求解**之前**决定放不放行，所以一次被拒绝的请求连请求体都没被读过，更谈不上改到状态。

公开面是 `pns/interfaces/security.py` 里一份**显式**清单，其余一切默认被保护——以后新加的
路由默认是保护的，不需要有人记得回来补：

| 公开 | 说明 |
|---|---|
| `GET /healthz` `GET /readyz` | 编排系统没有凭据，健康检查必须公开（见 6.4）|
| `GET /api/auth/session`、`POST /api/auth/login`、`POST /api/auth/logout` | 否则浏览器连"要不要登录"都问不出来 |
| `GET`/`HEAD` `/`、`/index.html`、`/favicon.svg`、`/icons.svg`、`/assets/*` | 前端外壳与静态资源，里面没有服务器侧秘密 |

被保护的因此包括：所有 `/api/**`（含只读的审核、World Editor 读、配置读）、`/ws/run`，
以及 FastAPI 自动挂的 `/openapi.json`、`/docs`、`/redoc`。**只读也保护**是一次显式分类，
不是顺手继承：这是一个操作者控制面，不是公开站点。

### 6.2 两种凭据、两种主体

**`Authorization: Bearer <PNS_ADMIN_TOKEN>`** —— 给 curl 和运维脚本用，认成一个稳定的
**非人类**主体（`principal_id` = `svc-break-glass`，`kind` = `service`，admin scope）。
scheme 大小写不敏感；比较走 `hmac.compare_digest`。出现**重复** `Authorization` 头一律拒绝，
就算其中一份是对的——两份凭据的请求没有唯一答案，不许挑一个能过的。

它**不接受从登录框进来**。让它同时当网页口令，等于把一把不属于任何人、不会过期、撤销要重启
进程的钥匙发给每个用浏览器的人，账户体系里的停用/改角色/改密码就全都绕得过去。它也不出现在
用户列表里，也没有密码可改。

**会话 Cookie `pns_session`** —— 给浏览器用，由用户名 + 密码换来。`HttpOnly`、
`SameSite=Strict`、`Path=/`，`Secure` 由 `PNS_SESSION_COOKIE_SECURE` 决定。每张会话记着签发
时的账户和**安全修订号**：停用、改角色、改密码都会推进那个数，于是所有旧会话在下一次请求就
对不上号——撤销不需要等 TTL，也不依赖一次成功的进程内通知。会话只活在内存里，进程重启就没了。

CSRF 有两把锁：`SameSite=Strict`（跨站请求带不上这张 Cookie），以及服务端对**所有**非安全
方法和 WebSocket 握手做的同源检查——有 `Origin` 头且与 `Host` 不同源就 403 `cross_origin`，
在认证之前。比较的是 host[:port]，**不比 scheme**：终结 TLS 的反向代理会让浏览器的
`https://` 撞上进程侧的 `http`，按完整源比较会让每一台正常的 TLS 部署全部写操作 403。
重复的 `Origin` 头和 `Origin: null` 都按跨源处理。反向代理必须透传原始 Host，或者把浏览器
实际访问的源写进 `PNS_TRUSTED_ORIGINS`。不发 `Origin` 的非浏览器客户端不受影响。

只要请求里出现了 `Authorization` 头，就**由它决定**，不会因为浏览器里还有一张有效 Cookie
而放行。否则"这次调用用的是哪个凭据"会变成一个说不清的问题。

管理 token 本身**不是**会话 id：拿 token 当 Cookie 值发过来会被拒绝。

### 6.2.1 角色与 scope

角色是**权限的捆绑**，路由问的永远是"有没有这个 scope"，不是"是不是管理员"：

| 角色 | scopes |
|---|---|
| `admin` | `read` `operate` `accounts:manage` |
| `operator` | `read` `operate` |
| `observer` | `read` |

授权判据来自**方法和路径**，不是路由挂没挂依赖：

- 安全方法（`GET`/`HEAD`/`OPTIONS`）要 `read`；
- 其余方法要 `operate`；
- 任何 WebSocket 要 `operate`（`/ws/run` 会花模型额度，它不该因为"不是 POST"就落进只读那档）；
- `/api/accounts/**` 在此之上再要 `accounts:manage`（挂在路由器上，新增路由自动继承）；
- 自服务清单（目前只有 `POST /api/auth/password`）只要求已认证，不要求任何 scope。

所以以后新加的那条 POST 默认对 `observer` 是关着的。权限不足返回
`403 {"detail": {"category": "forbidden", ...}}`。

### 6.3 `/api/auth/*`

| 接口 | 行为 |
|---|---|
| `GET /api/auth/session` | `{"mode", "auth_required", "authenticated", "principal"}`。`principal` 是 `{principal_id, username, kind, role, scopes, via}` 或 `null`；**不带任何凭据材料**，也不含密码哈希或安全修订号 |
| `POST /api/auth/login` | 请求体 `{"username", "password"}`；成功 200 并下发会话 Cookie；失败一律 401 `invalid_credential`——用户名不存在、密码不对、账户被停用三者的响应**逐字节相同**（区别只写进审计）；按账户分桶节流，失败够多返回 429 并带 `Retry-After`；这台服务器没有账户体系时 409 |
| `POST /api/auth/logout` | 作废当前会话并清 Cookie。公开是刻意的：登出不该需要先证明自己登着 |
| `POST /api/auth/password` | **需要已认证**（不在公开清单里）。请求体 `{"current_password", "new_password"}`；成功之后该账户的**全部会话（含当前这张）**立刻作废，响应 `authenticated: false`，前端退回登录框。旧密码不对返回 400，并留下一条审计。break-glass 和开放的开发主体没有密码可改，返回 409 `not_an_account` |

### 6.3.1 `/api/accounts/*`（需要 `accounts:manage`）

| 接口 | 行为 |
|---|---|
| `GET /api/accounts` | `{"users": [...]}`，只列人类账户；每条是 `{principal_id, username, kind, role, scopes, enabled, created_at, updated_at}` |
| `GET /api/accounts/audit?limit=` | `{"records": [...]}`，倒序。每条含时间、动作、结果、操作者/目标 principal（顺带翻成用户名）和一个结构化 `detail`。**没有凭据、没有哈希、没有原始异常，也不记尝试过的用户名** |
| `POST /api/accounts` | `{"username", "password", "role"}` → 201。用户名重复（含大小写/全角折叠之后重复）返回 409 `account_conflict` |
| `POST /api/accounts/{principal_id}/role` | `{"role"}`。改完立刻作废目标的全部会话，响应里带 `revoked_sessions` |
| `POST /api/accounts/{principal_id}/enabled` | `{"enabled"}`。同上 |
| `POST /api/accounts/{principal_id}/password` | `{"password"}`，管理员重置，不需要旧密码。同上 |

最后一个启用着的管理员不能被停用或降级：返回 409 `last_admin`。这条裁决发生在数据库写锁
之下，所以两个并发的降级请求不可能都通过。

用户名的判重只有一条规则：NFKC 归一 + `casefold`，且限制在 ASCII 字母数字和 `. _ -`。
限制字符集是这条规则的一部分——西里尔字母 `а` 会 casefold 成它自己，允许它就等于允许一个和
`admin` 并存、在任何界面上都看不出区别的账户。密码只存 Argon2id 哈希，长度 12–512，
**不做 strip 也不做归一化**（那是在悄悄改掉一个秘密）。

被拒绝的请求返回 `401` + `WWW-Authenticate: Bearer`，正文是
`{"detail": {"category": "unauthenticated", "message": "需要管理凭据"}}`，跟持久世界路由
同一种错误形状。WebSocket 在握手阶段直接关闭，浏览器拿不到一条已建立的连接。

### 6.4 `/healthz` 与 `/readyz`

两条都公开，因此它们同时满足两件事：正文里没有密钥、没有 provider 名、没有世界状态、没有路径；
并且**没有权威副作用**——不调用模型、不推进时间、不重载配置、不获取世界所有权、不建目录。

```
GET /healthz  →  {"status": "ok"}
GET /readyz   →  {"status": "ready", "mode": "production", "auth_required": true, "dashboard": true}
```

就绪之所以可以这么轻：**配置不可用的生产进程根本起不来**（见 6.5）。所以"起来了并且能应答"
就是"启动配置完成了"的充分证据。配置坏掉的表现是连接被拒绝，不是一个回答"我不太好"的 200。

### 6.5 生产模式

`PNS_ENV=production`（生产镜像在 Dockerfile 里固化了它）下，这三样缺一不可，缺任何一样
`create_app()` 直接抛、进程起不来：

1. `PNS_ADMIN_TOKEN`：≥32 字符、首尾无空白、不是示例占位串；
2. 模型 provider 凭据（`PNS_API_KEY_NAME` 指向的那个变量）非空；
3. 已构建的 `dashboard/dist`；
4. 账户库里至少一个**启用着的管理员**（见 `docs/DEPLOY_UBUNTU_DOCKER.md` 第 8 节）。

不存在"缺了就回落到开发模式"这条路。账户库打不开时也一样：那种情况**不**回落成"那就没有
账户"——那会把一台配好了账户的服务器悄悄变回谁都能进的服务器。

其它取值（默认 `development`）保持既有本地开发行为：既没配 `PNS_ADMIN_TOKEN`、账户库里也
没有账户时不鉴权。配了任意一样就一定强制——它不是开关。这台服务器要不要凭据在**启动时**
定下来，不随请求变化。

第一个管理员由 `PNS_BOOTSTRAP_ADMIN_USERNAME` + `PNS_BOOTSTRAP_ADMIN_PASSWORD_HASH`
（Argon2id 哈希，不是明文）创建，或者由 `scripts/accounts.py` 离线创建。引导是幂等的，而且
库里已经有任何主体时它什么都不做——所以它不是一条"改一行环境变量就重新拿到管理员"的提权路径。

### 6.6 生产模式下被拒绝的写接口

`POST /api/config`、`POST /api/world/scenes`、`/scenes/source`、`/facts`、`/facts/source`
在生产模式下返回 `409 immutable_deployment`。理由不是"生产要严一点"，而是那种写入在生产里
**没有意义且会骗人**：它们写的是镜像层里的 `pns/world/*.py` 和 `.env`，下一次容器重建就没了，
而且容器里的 `.env` 还会盖住 Compose 注入的配置。改法见 `docs/DEPLOY_UBUNTU_DOCKER.md`。

这道守卫是路由级依赖，跑在请求体校验之前：一份畸形请求体拿到的也是 409，而不是一句把 schema
讲出去的 422。对应的 `GET` 读接口不受影响。`POST /api/config/reload` 不写盘，在生产照常可用。

### 6.7 日志

进程的 stdout/stderr 被换成按行缓冲、按**值**遮蔽的流：`PNS_ADMIN_TOKEN`、
`PNS_BOOTSTRAP_ADMIN_PASSWORD_HASH` 与所有 provider key 变量的当前值在输出里被替换成
`***REDACTED***`。闸设在流上而不是做成 logging 过滤器，是因为
泄露最可能发生在异常路径——一条被打印的 traceback、SDK 在报错里回显的请求头、uvicorn 自己的
堆栈——那些都不经过应用的 logger。值在写的时候现取，所以一次配置重载换掉的 key 也照样被盖住。

两条它做不到的事，写在这里而不是假装没有：短于 8 个字符的值不遮蔽（否则会把日志本身抹成噪音）；
一个恰好被 flush 切成两半的密钥会漏（缓冲按行，正常日志行遇不到）。
