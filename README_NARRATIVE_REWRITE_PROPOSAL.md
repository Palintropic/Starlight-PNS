# PNS README 叙事重写提案

> 提案范围：仅替换 `README.md` 与 `README_CN.md` 中的 Overview / 概述、Core Architecture / 核心架构、Characters / 角色三个章节。其他章节保持不变。

## 1. 诊断与改写思路

当前 README 仍以绘名和瑞希的双人实验为叙事主线，但代码已经把角色数据拆成可插拔角色包，并让会话运行器支持从角色池中选择任意数量（至少两个）的角色进行轮转。因此，旧叙事会让读者把当前默认组合误认为框架本身的边界。

这份提案做三项调整：

1. **Overview 先定义通用研究问题。** 主语改为“可扩展的角色一致性框架”，保留封闭世界、宪法判官和防止漂移成通用助手三个核心概念；PJSK 则作为当前参考实现出现，而不是框架定义本身。
2. **Core Architecture 区分框架层与内容层。** 新图展示角色包如何经过运行时注册表形成可变角色池，由轮转调度器逐个调用，再由 Router 评估每一轮；同时明确世界容器提供边界，Router 将纠正反馈给后续轮次。
3. **Characters 先讲角色包，再讲具体角色。** 先说明 PJSK 包是可替换的内容实现，再按五个团体列出全部 20 个已注册角色及真实完成状态。绘名与瑞希的关系只保留为已完成角色简介中的一条内容层说明，不再用于解释系统架构。

措辞上会区分“架构支持”“角色已注册”和“角色已可运行”：当前角色包有 20 个注册角色，但只有绘名和瑞希处于 ready，奏和真冬为 partial，其余 16 个为 not_ready。

---

## 2. English replacement text

### Overview

```markdown
## Overview

PNS is a research framework built by a 16-year-old high school student exploring a simple but hard question:

**Can AI agents remain authentic to their fictional personas over time — without drifting into generic assistant behavior — when they live inside a closed world and a constitutional judge watches each turn?**

PNS treats persona consistency as a framework problem rather than a fixed cast experiment. Character data is loaded from a pluggable pack, and the session runner can rotate through a selected pool of two or more characters. Each character inhabits the same offline world container, where lore, schedules, relationships, locations, and the current scene limit what the agent should know and how it should behave. A Router model evaluates every turn against constitutional and character-specific constraints, records a drift score, and can feed a correction into the next turn.

The current reference implementation is the PJSK character pack: 20 characters across five units, with different levels of completion. PJSK is the first test world, not a limit of the architecture; the longer-term aim is to study the same consistency framework with other fictional or original character packs.

The working insight behind this project is that **CAI-trained models may resist out-of-character drift more naturally**, even under adversarial pressure. PNS provides a structured way to compare that behavior with non-CAI-trained control models.
```

### Core Architecture

````markdown
## Core Architecture

```
┌──────────────────────────────────────────────┐
│          Pluggable Character Pack            │
│  manifest · units · character YAML · prompts │
│  Current reference: PJSK, 20 registered      │
└──────────────────────┬───────────────────────┘
                       │ runtime discovery
             ┌─────────▼─────────┐
             │ Character Registry │
             └─────────┬─────────┘
                       │ select 2…N characters
          ┌────────────▼────────────┐
          │  Round-Robin Scheduler  │
          └────────────┬────────────┘
                       │ one character per turn
      ┌────────────────▼────────────────┐
      │     Selected Character Pool     │
      │  Agent A · Agent B · … · Agent N │
      │          (stateless)            │
      └───────────┬───────────▲─────────┘
                  │ output     │ next-turn correction
                  ▼            │
      ┌─────────────────────────────────┐
      │         Router-as-Judge         │
      │ constitutional evaluation · OOC │
      │ detection · drift score (0–10)  │
      └─────────────────────────────────┘

┌──────────────────────────────────────────────┐
│             Closed-World Container           │
│ lore · schedules · relationships · locations │
│ scenes · no external information access      │
└──────────────────────┬───────────────────────┘
                       └──── constrains every agent turn
```

The framework and the content pack are separate. The framework discovers characters from the active pack, the researcher selects a pool for a session, and the scheduler rotates through that pool. The closed world supplies the context and information boundary for every character turn. The Router evaluates each output and, when needed, sends a correction back into the following turn.

### Key Components

**1. Closed-World Constraint**  
Agents operate entirely within the active fictional world. Lore, daily schedules, relationship states, location constraints, and scene state are injected as behavioral and informational boundaries; agents do not have external information access.

**2. Life Simulation (not dialogue simulation)**  
Unlike chatbot-style frameworks, PNS simulates life unfolding over time. Characters may attend school, work, create alone, or meet one another. Interaction is contextual and occasional rather than a requirement to keep chatting continuously.

**3. Pluggable Character Packs**  
Character and unit data live outside the core framework in a pack manifest, YAML metadata, and prompt files. The active pack determines which characters exist, while completion status determines which of them can currently run. Version 1 loads one active pack at a time.

**4. N-Character Rotation**  
A session selects two or more registered characters. The runner keeps a separate history for each character, shares each turn with the other selected characters, and rotates through the pool without hard-coding a particular pair.

**5. Constitutional AI Alignment**  
A four-layer constitutional document defines hard constraints, soft defaults, character-specific persona constraints, and drift-detection rules with a 0–10 scoring rubric.

**6. Router-as-Judge**  
The Router monitors every turn against constitutional ground truth. It records persona drift and can generate a correction for the next turn when intervention is needed. It also serves as the system's controlled information gateway, with the researcher remaining the top-level trust layer.
````

### Characters

```markdown
## Characters

PNS does not define its cast in framework code. Characters are discovered from the active, pluggable character pack. A pack contains its manifest, unit structure, character metadata, dialogue research, and system prompts, so the content layer can develop independently from the simulation and evaluation framework.

The current reference implementation is `packs/pjsk/`, which registers 20 characters from the PJSK universe across five units. Registration means that a character exists in the pack; it does not necessarily mean that the character has enough research and prompt material to run yet.

Status legend: ✅ ready to run · 🟡 partial metadata, prompt not yet available · ⚪ registered, not yet built out

### 25-ji, Nightcord de.

**ena (東雲絵名)** ✅  
Illustrator and night-class high school student. Her current character sheet includes her reversed schedule, core personality, relationships, and tone research.

**mizuki (暁山瑞希)** ✅  
Video animator and high school student. Their current character sheet includes their flexible daytime schedule, part-time work, late-night group activity, relationships, and tone research.

As one relationship detail inside this content pack, ena and mizuki often meet late at night on Nightcord and may occasionally cross paths around school; this is part of their character context, not a limit on which characters the framework can simulate.

**kanade (宵崎奏)** 🟡 — Composer. Basic metadata exists; dialogue research and a runnable prompt are not yet complete.  
**mafuyu (朝比奈真冬)** 🟡 — Lyricist. Basic metadata exists; dialogue research and a runnable prompt are not yet complete.

### Vivid BAD SQUAD

**akito (東雲彰人)** ⚪ · **an (白石杏)** ⚪ · **toya (青柳冬弥)** ⚪ · **kohane (小豆泽心羽)** ⚪  
Registered in the pack with basic metadata; runnable prompts have not yet been created.

### Wonderlands×Showtime

**tsukasa (天馬司)** ⚪ · **emu (鳳笑梦)** ⚪ · **nene (草薙寧々)** ⚪ · **rui (神代類)** ⚪  
Registered in the pack with basic metadata; runnable prompts have not yet been created.

### MORE MORE JUMP!

**minori (花里实乃理)** ⚪ · **haruka (桐谷遥)** ⚪ · **airi (桃井愛莉)** ⚪ · **shizuku (日野森雫)** ⚪  
Registered in the pack with basic metadata; runnable prompts have not yet been created.

### Leo/need

**ichika (星乃一歌)** ⚪ · **saki (天馬咲希)** ⚪ · **honami (望月穗波)** ⚪ · **shiho (日野森志步)** ⚪  
Registered in the pack with basic metadata; runnable prompts have not yet been created.
```

---

## 3. 中文替换文本

### 概述

```markdown
## 概述

PNS 是一个由 16 岁高中生构建的研究框架，探索一个简单但困难的问题：

**如果让 AI 智能体生活在封闭世界中，并由宪法判官监督每一轮交互，它们能否长期忠于各自的虚构人格，而不漂移成通用助手？**

PNS 把角色一致性视为一个框架问题，而不是一组固定角色的实验。角色数据从可插拔的角色包中加载，会话运行器可以让选定角色池中的两个或更多角色依次行动。每个角色都生活在同一个离线世界容器中；世界观、作息、关系、地点和当前场景共同限制智能体应当知道什么、应当如何行动。Router 模型依据宪法约束和角色专属约束评估每一轮，记录漂移分数，并可以把纠正内容注入后续轮次。

当前的参考实现是 PJSK 角色包：五个团体、共 20 个角色，完成度各不相同。PJSK 是第一个测试世界，而不是架构的边界；长期目标是用其他虚构角色或原创角色包研究同一套一致性框架。

这个项目背后的工作假设是：**经过 CAI 训练的模型可能更自然地抵抗角色外漂移**，即使面对对抗性压力也是如此。PNS 为它们与未经 CAI 训练的对照模型之间的比较提供一个结构化实验环境。
```

### 核心架构

````markdown
## 核心架构

```
┌──────────────────────────────────────────────┐
│               可插拔角色包                   │
│   清单 · 团体 · 角色 YAML · 角色提示词       │
│   当前参考实现：PJSK，已注册 20 个角色        │
└──────────────────────┬───────────────────────┘
                       │ 运行时发现
             ┌─────────▼─────────┐
             │     角色注册表     │
             └─────────┬─────────┘
                       │ 选择 2…N 个角色
          ┌────────────▼────────────┐
          │       轮转调度器         │
          └────────────┬────────────┘
                       │ 每轮选择一个角色
      ┌────────────────▼────────────────┐
      │          当前角色池             │
      │  智能体 A · 智能体 B · … · 智能体 N │
      │            （无状态）           │
      └───────────┬───────────▲─────────┘
                  │ 输出       │ 后续轮次纠正
                  ▼            │
      ┌─────────────────────────────────┐
      │          Router 判官            │
      │ 宪法评估 · OOC 检测 · 漂移评分   │
      │             （0–10）            │
      └─────────────────────────────────┘

┌──────────────────────────────────────────────┐
│               封闭世界容器                   │
│ 世界观 · 作息 · 关系 · 地点 · 场景           │
│              无外部信息访问                  │
└──────────────────────┬───────────────────────┘
                       └──── 约束每个角色的行动轮次
```

框架层与内容包彼此分离。框架从当前角色包中发现角色，研究者为一次会话选择角色池，调度器再依次调用其中的角色。封闭世界为每一轮提供情境和信息边界；Router 评估每次输出，并在需要时把纠正内容反馈到后续轮次。

### 关键组件

**1. 封闭世界约束**  
智能体完全在当前启用的虚构世界中运行。世界观、日常作息、关系状态、地点约束和场景状态在初始化时注入，成为行为和信息边界；智能体无法访问外部信息。

**2. 生活模拟（而非对话模拟）**  
与聊天机器人式框架不同，PNS 模拟随时间展开的生活。角色可能上学、工作、独自创作，也可能彼此相遇。交互由情境自然产生，并不要求角色持续对话。

**3. 可插拔角色包**  
角色和团体数据不写在核心框架里，而是存放在角色包清单、YAML 元数据和提示词文件中。当前启用的角色包决定有哪些角色，角色的完成状态则决定它们目前能否实际运行。v1 同一时间只加载一个角色包。

**4. N 角色轮转**  
一次会话可以选择两个或更多已注册角色。运行器为每个角色分别保存历史，把每一轮内容同步给其他入选角色，并在角色池中循环调度，不再写死某一对角色。

**5. 宪法 AI 对齐**  
四层宪法文件定义硬约束、软性默认规则、角色专属人格约束，以及采用 0–10 评分标准的漂移检测规则。

**6. Router-as-Judge**  
Router 依据宪法基准监督每一轮，记录人格漂移，并在需要介入时为后续轮次生成纠正内容。它同时充当受控的信息网关，而研究者仍是整个系统最上层的信任层。
````

### 角色

```markdown
## 角色

PNS 不在框架代码中定义固定角色阵容。系统从当前启用的可插拔角色包中发现角色。一个角色包包含清单、团体结构、角色元数据、对话研究和 system prompt，因此内容层可以独立于模拟与评估框架逐步完善。

当前的参考实现是 `packs/pjsk/`，其中注册了 PJSK 世界五个团体的 20 个角色。“已注册”只表示角色存在于角色包中，并不代表它已经积累了足够的研究资料和提示词、可以实际运行。

状态说明：✅ 可运行 · 🟡 基础资料部分完成，运行提示词尚不可用 · ⚪ 已注册，尚未展开建设

### 25時、ナイトコードで。

**绘名（東雲絵名 / ena）** ✅  
插画负责人、夜间定时制高中生。当前角色档案已经包含昼夜颠倒的作息、核心性格、角色关系和语气研究。

**瑞希（暁山瑞希 / mizuki）** ✅  
动画负责人、高中生。当前角色档案已经包含较自由的白天作息、兼职和深夜团体活动，以及角色关系和语气研究。

作为这个内容包中的一条角色关系细节，绘名与瑞希常在深夜的 Nightcord 上相遇，也可能偶尔在学校附近碰面；这只是两人的角色语境，并不限制框架可以模拟哪些角色。

**奏（宵崎奏 / kanade）** 🟡 — 作曲人。基础元数据已经存在，对话研究和可运行提示词尚未完成。  
**真冬（朝比奈真冬 / mafuyu）** 🟡 — 作词人。基础元数据已经存在，对话研究和可运行提示词尚未完成。

### Vivid BAD SQUAD

**彰人（東雲彰人 / akito）** ⚪ · **杏（白石杏 / an）** ⚪ · **冬弥（青柳冬弥 / toya）** ⚪ · **心羽（小豆泽心羽 / kohane）** ⚪  
已在角色包中登记基础元数据；可运行提示词尚未创建。

### Wonderlands×Showtime

**司（天馬司 / tsukasa）** ⚪ · **笑梦（鳳笑梦 / emu）** ⚪ · **宁宁（草薙寧々 / nene）** ⚪ · **类（神代類 / rui）** ⚪  
已在角色包中登记基础元数据；可运行提示词尚未创建。

### MORE MORE JUMP!

**实乃理（花里实乃理 / minori）** ⚪ · **遥（桐谷遥 / haruka）** ⚪ · **爱莉（桃井爱莉 / airi）** ⚪ · **雫（日野森雫 / shizuku）** ⚪  
已在角色包中登记基础元数据；可运行提示词尚未创建。

### Leo/need

**一歌（星乃一歌 / ichika）** ⚪ · **咲希（天马咲希 / saki）** ⚪ · **穗波（望月穗波 / honami）** ⚪ · **志步（日野森志步 / shiho）** ⚪  
已在角色包中登记基础元数据；可运行提示词尚未创建。
```
