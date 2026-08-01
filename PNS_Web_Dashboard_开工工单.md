# PNS Web Dashboard 开工工单

**日期**：2026-07-29（确认版）
**负责**：Claude Code（CC）执行 / Mizuki 决策 / 本对话线分析支持

---

## 1. 背景与定位

Project Nightcord Sanctuary (PNS) 需要一个人工审核界面，用于查看 Router 打分结果，并对每轮对话做人工决策（通过/拒绝/重写）。

**前端顺序确认**：
- ✅ **优先做**：React Web Dashboard
- ⏸️ **推后**：iOS App（Expo/React Native），保留原有 `pns/app/` 结构不动，等 Web 版跑通后再回头做
- 两者最终都要做，当前只推进 Web 端

---

## 2. 核心功能（三栏式界面）

| 栏目 | 内容 |
|---|---|
| **① 对话展示** | turn-by-turn 展示当前 session 的完整对话 |
| **② Router 打分** | 显示 drift_score + 打分原因（Router 给出的诊断文本） |
| **③ 人工决策** | Mizuki 对每个 turn 做出：通过 / 拒绝 / 需要重写 |

---

## 3. 数据来源

- 读取本地 `drift_scores.jsonl`（`run_local.py` 已实现持久化 logging）
- 字段包含：`turn`、`drift_score`、打分原因等（具体字段需在开工前核对一次实际文件结构）

---

## 4. 待确认事项（开工前需要 Mizuki 或 CC 决定）

- [ ] **运行环境**：本地开发预览 vs. 部署到 ESXi VM 长期跑
- [ ] **审批操作是否落地**：点击"通过/拒绝/重写"后，是否要：
  - 仅做 UI 层标记（不改文件）
  - 还是要写回 `drift_scores.jsonl` 或新建决策记录文件
  - 是否需要触发后续动作（如 POST 到 WordPress via `pns_bot`）
- [ ] **`drift_scores.jsonl` 实际字段结构**：需要一份样本数据核对字段名，确保 Dashboard 展示字段与实际 log 对齐

---

## 5. 明确不做的事（本阶段范围外）

- 不接 iOS/Expo 开发
- 不做 WordPress 自动发布集成（API credentials 未就绪）
- 不做 Turn-10 自动 drift 注入相关功能

---

## 6. 下一步

1. 核对 `drift_scores.jsonl` 实际字段结构（样本数据）
2. 确定运行环境（本地 vs VM）
3. 确定审批操作的落地范围
4. CC 开始搭建 React Web Dashboard 骨架（三栏布局 + 读取 jsonl 展示）

---

*Powered by Project Nightcord Sanctuary / Design for Project Starlight*
