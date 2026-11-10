# bench/ — BENCH-1 评估 harness（Issue #43）。
#
# 它是运行时**之外**的评估层：只许 import pns/ 的公开入口（生成、判分、会话
# 状态与提交边界），pns/ 的任何模块都不许反过来 import 这里。三个条件共用
# 同一条生成链（pns.logic.simulation.call_character），差别只在上下文组装。
