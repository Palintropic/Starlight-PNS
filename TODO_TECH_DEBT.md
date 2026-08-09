# 技术债务清单

## compat（flash-lite）prompt 目前只覆盖 ena/mizuki 两人

**位置**：`packs/pjsk/characters/*.yaml` 的 `prompt_file_compat` 字段

**背景**：compat（叙事框架，适配 Gemini/flash-lite 等严格安全策略模型）prompt 已经
是 pack 化数据了（`prompt_file_compat` 字段 + `registry.get_character_prompt_compat()`），
不再硬编码在框架代码里。但内容本身只给 ena/mizuki 写过，其余 18 人的 yaml 没有这个
字段——`get_character_system(compat=True)` 找不到字段/文件时会自动回退到普通
`prompt_file`，所以不会报错或阻断 N 人轮转，只是那 18 人跑 flash-lite 系模型时用不到
叙事框架包装。

**何时处理**：跟角色内容本身（`_prompt.md`）一起按需补齐即可，不是架构问题了，
纯粹是内容覆盖率问题。
