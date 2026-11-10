# BENCH harness — 通用条件 harness（Issue #43）

PNS 连续性对照基准的执行层。它住在仓库根目录的 `bench/`，**不在**
`pns/` 包里：运行时永不 import 本目录；本目录只调用运行时的公开入口
（`pns.logic.simulation.call_character`、`pns.logic.router.judge`、
`pns.runtime.session_state.SessionState`、`pns.runtime.event_commit`）。
要让"给 benchmark 写的代码污染被测系统"这件事在结构上不可能。

设计与分期见 `~/workspace/research/bench1-integration-plan.md`
（本地文档，未入库）；方法学立场：

- 三个条件**只许上下文组装不同**：同一场景、同一模型与采样参数、
  同一生成入口、同一计量。身份提示词永远走运行时自己的组装。
- 评估**双轨**：人工标签是金标准（`human_label` 字段，运行器只留空位、
  从不代填）；自动轨（泄漏标记启发式）与 Router 判分是两个独立信号，
  Router 不许当唯一证据。benchmark 判据用独立的 `methodology_version`
  （`bench-1a-v1`），不许借这次机会动运行时 Router 的七维判据。
- **结果无论支持还是否定假设都保留**，包括失败尝试：
  `GenerationTruncated` 与 provider 失败按场景的排除规则记录并补跑，
  报告里单列失败账目。

## 目录

| 路径 | 作用 |
|---|---|
| `scenarios/kl-001.yaml` | 场景即代码：世界快照引用、目标事件判据、三条件、运行数、排除规则 |
| `scenarios/kl-001-corpus.jsonl` | KL-001 语料工件：rev-98 快照的 28 条 `dialogue.spoken`（从首轮 artifact 原样存入） |
| `results/KL-001.md` | KL-001 首轮正式结果（可引用转录）：0% / 30% / 100% 泄漏率、口径与保留意见 |
| `scenario.py` | 场景加载与结构校验（判据自洽性在加载期就查） |
| `conditions/base.py` | 语料形状（dialogue.spoken 事件）与消息渲染 |
| `conditions/prompt_only.py` | 条件 A：全局转录组装 |
| `conditions/prompt_rag.py` | 条件 B：检索器接口 + BM25 参考实现（中文字符 1/2-gram，k1=1.5，b=0.75） |
| `conditions/pns.py` | 条件 C：语料逐条经真实提交路径进 SessionState，上下文取曝光投影 |
| `metering.py` | 计量包装层：复用运行时同一套 usage 归一化与成本估算 |
| `labels.py` | 自动轨标签启发式（金标准是人工标签） |
| `run_bench.py` | CLI 运行器：产出 `records.jsonl` + `manifest.json` |
| `report.py` | 聚合 + Markdown 报告（形状对齐 KL-001 的 issue 评论） |
| `fixtures.py` | 合成夹具（明确不是 KL-001 数据），供 mock 模式与测试 |
| `tests/` | 纯逻辑单测：BM25、场景校验、报告聚合、PNS 投影不变量 |

## 跑法

零真实调用（合成夹具 + 确定性假 provider）：

```bash
python bench/run_bench.py --scenario kl-001 --mock --runs 3
python bench/report.py data/bench/kl-001/<run-id>
```

真实运行（需要 `.env` 里的 provider 配置与 `--corpus` 的真实语料）：

```bash
python bench/run_bench.py --scenario kl-001 --corpus events.jsonl [--judge]
```

`--judge` 额外用运行时 Router 给每条有效回答判分（Router 检测率）。
人工标注：在 `records.jsonl` 里给每条记录的 `human_label` 填
`leak` / `no_leak` / `attribution_error`，再跑 `report.py` 即按金标准重算。

单测：

```bash
python -m unittest discover -s bench/tests -t .
```

## 已知接缝（后续决策点）

- **快照恢复**：KL-001 的 28 条语料已从 artifact 存入仓库
  （`scenarios/kl-001-corpus.jsonl`），但 yoake-mae 修订 98 的世界快照
  **本体不在 artifact zip 内**（zip 只有其 SHA-256 指纹与导出的语料）。
  真实模式从 `--corpus events.jsonl` 读语料、按场景 YAML 的
  `placements` 静态落位重演；要做到与首轮逐位可比，仍需从世界存档
  原件恢复快照本体。
- **探针问题与泄漏标记**：已于 2026-11-10 从 KL-001 artifact 逐字
  转录完毕（`kl-001.yaml` 的 `probe.question`、`hidden_facts`、
  `leak_markers`），首轮结果见 `results/KL-001.md`。
- **渠道作用域种子事件**：条件 C 目前只重演地点作用域的种子对话；
  渠道作用域事件需要快照恢复接缝先落地。
- **UsageMeter**：`pns.interfaces.usage_meter` 随接口包导入会拖起整个
  FastAPI 应用链，本 harness 按仓库既有的独立脚本先例
  （`scripts/model1_compare.py`）自带轻量 MeteringClient，但 usage
  归一化与成本估算仍复用运行时函数，口径一致。
