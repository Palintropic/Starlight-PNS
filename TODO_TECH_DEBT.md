# 技术债务清单

## 角色框架重构遗留的过渡别名

**位置**：`pns/world/__init__.py` 底部

```python
def get_ena_system(scene): return get_character_system('ena', scene)
def get_mizuki_system(scene): return get_character_system('mizuki', scene)
def get_ena_system_compat(scene): return get_character_system('ena', scene, compat=True)
def get_mizuki_system_compat(scene): return get_character_system('mizuki', scene, compat=True)
```

**背景**：角色框架重构（`characters/` 按团分层 + registry 角色ID改名 mzk→mizuki）把角色prompt的获取入口统一成了 `get_character_system(character_id, scene, compat=False)`。但 `server.py` 的 `run_simulation` 还是写死的二人轮转状态机（`current` 只在 `"mizuki"`/`"ena"` 之间切换，非N人），且 `import` 的是这四个旧函数名，所以暂时保留它们作为过渡别名，让 `server.py` 不用跟着这次重构一起改。

**何时删除**：`server.py` 的 N人群聊轮转重构（下一阶段）完成、`run_simulation` 改用 `get_character_system()` 统一入口之后，直接删掉这四个别名和对应的 `server.py` import。

**关联**：`server.py` 里 `histories`/`current`/`char_name`/`other` 已经在角色ID改名的同一批里从 `"mzk"` 换成了 `"mizuki"`（保持二元 if/else 结构不变），`call_character_async()` 里调用的也已经同步改成 `get_mizuki_system`/`get_mizuki_system_compat`——但这两个函数本质上还是"硬编码指向mizuki一个人"的过渡写法，这也是它们暂时还不能删的原因。
