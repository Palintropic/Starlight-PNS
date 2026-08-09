# pns/world/characters/registry.py
"""角色数据的运行时加载入口。

不再硬编码角色表：所有角色/团体数据来自 packs/<ACTIVE_PACK>/ 下的
pack.yaml + units/*.yaml + characters/<unit_id>/*.yaml +
characters/<unit_id>/*_prompt.md，按团分子目录存放，按 kickoff/PACK_SPEC_v1.md
描述的格式加载。框架代码本身不含任何 PJSK 专属字符串（角色名、团名等）。

v1 只支持单一 active pack（见 PACK_SPEC_v1.md §8），先写死为 'pjsk'。
"""
from pathlib import Path
from typing import Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKS_ROOT = REPO_ROOT / "packs"
ACTIVE_PACK = "pjsk"

_cache: Optional[Dict] = None


class CharacterNotReadyError(Exception):
    """角色存在于 pack 里，但还没有可用的 system prompt（status=partial/not_ready 且未补内容）。"""

    def __init__(self, character_id: str, detail: str):
        self.character_id = character_id
        self.detail = detail
        super().__init__(detail)


def _read_yaml(path: Path) -> Dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_pack() -> Dict:
    """读取并校验 active pack，结果按进程缓存一次。"""
    global _cache
    if _cache is not None:
        return _cache

    pack_dir = PACKS_ROOT / ACTIVE_PACK
    manifest_path = pack_dir / "pack.yaml"
    if not manifest_path.exists():
        raise ValueError(f"Pack manifest not found: {manifest_path}")
    manifest = _read_yaml(manifest_path)

    units: Dict[str, Dict] = {}
    for unit_id in manifest.get("units", []):
        unit_path = pack_dir / "units" / f"{unit_id}.yaml"
        if not unit_path.exists():
            raise ValueError(
                f"Pack '{ACTIVE_PACK}' 声明了 unit '{unit_id}'，但找不到 {unit_path}"
            )
        units[unit_id] = _read_yaml(unit_path)

    characters: Dict[str, Dict] = {}
    for entry in manifest.get("characters", []):
        char_id, unit_id = entry["id"], entry["unit"]
        if unit_id not in units:
            raise ValueError(
                f"角色 '{char_id}' 的 unit '{unit_id}' 不在 pack.yaml 的 units 列表里"
            )
        char_path = pack_dir / "characters" / unit_id / f"{char_id}.yaml"
        if not char_path.exists():
            raise ValueError(
                f"pack.yaml 声明了角色 '{char_id}'，但找不到 {char_path}"
            )
        data = _read_yaml(char_path)
        if data.get("unit") != unit_id:
            raise ValueError(
                f"角色 '{char_id}' 在 pack.yaml 里的 unit 是 '{unit_id}'，"
                f"但 {char_path} 里写的是 '{data.get('unit')}'"
            )
        characters[char_id] = data

    _cache = {"manifest": manifest, "units": units, "characters": characters, "pack_dir": pack_dir}
    return _cache


def get_character_prompt(character_id: str) -> str:
    """获取角色的 SYSTEM prompt，从 pack 里的 prompt_file 读取"""
    pack = _load_pack()
    if character_id not in pack["characters"]:
        raise ValueError(f"Character not found in registry: {character_id}")

    info = pack["characters"][character_id]
    prompt_file = info.get("prompt_file")
    if not prompt_file:
        raise ValueError(f"角色 {character_id} 未声明 prompt_file")

    prompt_path = pack["pack_dir"] / "characters" / info["unit"] / prompt_file
    if not prompt_path.exists():
        raise ValueError(
            f"角色 {character_id}（status={info.get('status')}）尚无 system prompt，"
            f"预期路径 {prompt_path.relative_to(REPO_ROOT)}"
        )
    return prompt_path.read_text(encoding="utf-8").strip()


def get_character_prompt_compat(character_id: str) -> Optional[str]:
    """获取角色的 compat（叙事框架）prompt，不存在则返回 None（正常情况，非错误）"""
    pack = _load_pack()
    if character_id not in pack["characters"]:
        raise ValueError(f"Character not found in registry: {character_id}")

    info = pack["characters"][character_id]
    prompt_file = info.get("prompt_file_compat")
    if not prompt_file:
        return None

    prompt_path = pack["pack_dir"] / "characters" / info["unit"] / prompt_file
    if not prompt_path.exists():
        return None

    return prompt_path.read_text(encoding="utf-8").strip()


def get_character_metadata(character_id: str) -> Dict:
    """获取角色的元数据"""
    pack = _load_pack()
    if character_id not in pack["characters"]:
        raise ValueError(f"Character not found: {character_id}")
    return pack["characters"][character_id].copy()


def list_characters(include_partial: bool = False, include_not_ready: bool = False) -> Dict[str, Dict]:
    """列出所有可用角色"""
    pack = _load_pack()
    result = {}
    for char_id, info in pack["characters"].items():
        if info["status"] == "ready":
            result[char_id] = info
        elif include_partial and info["status"] == "partial":
            result[char_id] = info
        elif include_not_ready and info["status"] == "not_ready":
            result[char_id] = info
    return result


def get_available_pairs() -> List[tuple]:
    """获取所有ready状态的角色组合"""
    ready_chars = list_characters(include_partial=False)
    pairs = []
    ids = list(ready_chars.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            pairs.append((ids[i], ids[j]))
    return pairs
