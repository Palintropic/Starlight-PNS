# 技术债务清单

## compat（flash-lite）prompt 目前只覆盖 ena/mizuki 两人

**位置**：`pns/world/__init__.py` 的 `_COMPAT_PROMPTS` 字典 + `get_character_system(character_id, scene, compat=True)`

**背景**：`_COMPAT_PROMPTS` 是硬编码在 `pns/world/__init__.py` 里的 dict，只给 ena/mizuki
手写过叙事框架版 system prompt（为适配 Gemini/flash-lite 等严格安全策略模型）。
`get_character_system()` 本身已经是通用入口——`compat=True` 且角色不在 `_COMPAT_PROMPTS`
里时会自动回退到 registry 里的普通 `get_character_prompt()`，所以 N 人轮转不会因为
compat 缺失而崩，只是那些角色跑 flash-lite 系模型时用不到叙事框架包装。

**何时处理**：要不要把 compat prompt 也纳入 pack schema（比如给
`packs/<pack>/characters/<id>.yaml` 加一个 `prompt_file_compat` 字段，从文件读取而不是
硬编码在框架代码里），这个设计问题还没定，先记录，不要在补角色内容时顺手加。
