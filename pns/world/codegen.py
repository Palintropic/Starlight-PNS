# pns/world/codegen.py
# World Editor 的写回逻辑：JSON（前端表单/源码编辑器）⇄ Python 源码。
# 只替换 scenes.py / facts.py 里目标变量的赋值节点，文件头注释和其余顶层语句原样保留。
import ast
import json
import shutil
from pathlib import Path

import black

SCENES_PATH = Path(__file__).parent / "scenes.py"
FACTS_PATH = Path(__file__).parent / "facts.py"

# facts.py 的分组展示锚点（对应工单3.3列的7个分组）。这里是唯一维护分组的地方——
# 写回时按这份映射重新生成 `# ─── 分组名 ───` 注释，新 key 落入"未分组"，不会因为
# 保存而把手写的分组注释冲掉。
FACT_GROUPS: dict[str, list[str]] = {
    "学校/作息": [
        "school", "ena_schedule", "mzk_schedule",
        "intersection_daytime", "intersection_night", "class_2b_classmate",
    ],
    "性别认同注记": ["mizuki_gender_note"],
    "25ji": [
        "25ji_shinonome_ena", "25ji_akiyama_mizuki",
        "25ji_hiiragi_kanade", "25ji_asahi_mafuyu",
    ],
    "VBS": [
        "vbs_shinonome_akito", "vbs_shiraishi_an",
        "vbs_aoyagi_toya", "vbs_azusawa_kohane",
    ],
    "WxS": [
        "wxs_tenma_tsukasa", "wxs_otori_emu",
        "wxs_kusanagi_nene", "wxs_kamishiro_rui",
    ],
    "MMJ": [
        "mmj_hanasato_minori", "mmj_kiritani_haruka",
        "mmj_momoi_airi", "mmj_hinomori_shizuku",
    ],
    "Leo/need": [
        "leoneed_hoshino_ichika", "leoneed_tenma_saki",
        "leoneed_mochizuki_honami", "leoneed_hinomori_shiho",
    ],
}

SCENE_FIELD_ORDER = [
    "id", "label", "time", "location", "weather", "day_phase", "scene_type",
    "lore_tag", "trigger", "gate_triggers", "gate_opening_note",
    "auto_next", "auto_turns",
]
# 这两个字段本来就不是每个 scene 都有（只有 gate 场景带），值为 None/缺失时整个 key 都不写
OPTIONAL_SCENE_FIELDS = {"gate_triggers", "gate_opening_note"}


class CodegenError(ValueError):
    """写回 scenes.py / facts.py 过程中的校验失败（语法错误、变量缺失、类型不对等）。"""


def _lit(value, indent: int) -> str:
    """把一个 JSON 兼容的 Python 值转成合法的 Python 字面量源码（缩进仅用于可读性，
    最终整个文件会过一遍 black 重新排版，所以这里不需要追求精确对齐）。"""
    pad = " " * indent
    pad_close = " " * max(indent - 4, 0)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for k, v in value.items():
            lines.append(f"{pad}{json.dumps(str(k), ensure_ascii=False)}: {_lit(v, indent + 4)},")
        lines.append(f"{pad_close}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for v in value:
            lines.append(f"{pad}{_lit(v, indent + 4)},")
        lines.append(f"{pad_close}]")
        return "\n".join(lines)
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def _serialize_scene_entry(scene: dict, indent: int) -> str:
    pad = " " * indent
    pad_close = " " * max(indent - 4, 0)
    lines = ["{"]
    for field in SCENE_FIELD_ORDER:
        if field not in scene:
            continue
        value = scene[field]
        if field in OPTIONAL_SCENE_FIELDS and value is None:
            continue
        lines.append(f"{pad}{json.dumps(field, ensure_ascii=False)}: {_lit(value, indent + 4)},")
    lines.append(f"{pad_close}}}")
    return "\n".join(lines)


def serialize_scenes(scenes: dict) -> str:
    lines = ["{"]
    for scene_id, scene in scenes.items():
        entry = _serialize_scene_entry(scene, 8)
        lines.append(f"    {json.dumps(str(scene_id), ensure_ascii=False)}: {entry},")
    lines.append("}")
    return "\n".join(lines)


def serialize_facts(facts: dict) -> str:
    grouped_keys = {k for keys in FACT_GROUPS.values() for k in keys}
    lines = ["{"]
    for group_name, keys in FACT_GROUPS.items():
        present = [k for k in keys if k in facts]
        if not present:
            continue
        lines.append(f"    # ─── {group_name} ───")
        for k in present:
            lines.append(f"    {json.dumps(k, ensure_ascii=False)}: {_lit(facts[k], 4)},")
    leftover = [k for k in facts if k not in grouped_keys]
    if leftover:
        lines.append("    # ─── 未分组 ───")
        for k in leftover:
            lines.append(f"    {json.dumps(k, ensure_ascii=False)}: {_lit(facts[k], 4)},")
    lines.append("}")
    return "\n".join(lines)


def _replace_assignment(original_source: str, var_name: str, new_value_src: str) -> str:
    """只替换 `var_name = ...` 这一顶层赋值的源码范围，其余文件内容（头部注释、
    其他顶层变量）原样保留。"""
    tree = ast.parse(original_source)
    target = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == var_name for t in node.targets
        ):
            target = node
            break
    if target is None:
        raise CodegenError(f"源文件里找不到顶层赋值 {var_name}")

    lines = original_source.splitlines(keepends=True)
    before = lines[: target.lineno - 1]
    after = lines[target.end_lineno:]
    return "".join(before) + f"{var_name} = {new_value_src}\n" + "".join(after)


def format_source(code: str) -> str:
    try:
        return black.format_str(code, mode=black.Mode())
    except Exception as e:
        raise CodegenError(f"生成的代码格式化失败：{e}") from e


def validate_module_source(source: str, expected_name: str) -> dict:
    """校验一段源码语法合法、且顶层定义了 expected_name 且类型是 dict。
    用受限命名空间执行（禁用 builtins），只允许字面量赋值，防止意外/恶意代码执行。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise CodegenError(f"Python 语法错误：{e}") from e

    namespace: dict = {}
    try:
        exec(compile(tree, "<world-editor>", "exec"), {"__builtins__": {}}, namespace)
    except Exception as e:
        raise CodegenError(f"源码执行失败：{e}") from e

    if expected_name not in namespace:
        raise CodegenError(f"源码里找不到变量 {expected_name}")
    value = namespace[expected_name]
    if not isinstance(value, dict):
        raise CodegenError(f"{expected_name} 必须是一个 dict，实际是 {type(value).__name__}")
    return value


def backup_file(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))


def build_scenes_source(scenes: dict) -> str:
    original = SCENES_PATH.read_text(encoding="utf-8")
    spliced = _replace_assignment(original, "SCENES", serialize_scenes(scenes))
    return format_source(spliced)


def build_facts_source(facts: dict) -> str:
    original = FACTS_PATH.read_text(encoding="utf-8")
    spliced = _replace_assignment(original, "WORLD_FACTS", serialize_facts(facts))
    return format_source(spliced)


def save_scenes(scenes: dict) -> None:
    new_source = build_scenes_source(scenes)
    validate_module_source(new_source, "SCENES")
    backup_file(SCENES_PATH)
    SCENES_PATH.write_text(new_source, encoding="utf-8")


def save_facts(facts: dict) -> None:
    new_source = build_facts_source(facts)
    validate_module_source(new_source, "WORLD_FACTS")
    backup_file(FACTS_PATH)
    FACTS_PATH.write_text(new_source, encoding="utf-8")


def save_scenes_source(source: str) -> None:
    validate_module_source(source, "SCENES")
    formatted = format_source(source)
    backup_file(SCENES_PATH)
    SCENES_PATH.write_text(formatted, encoding="utf-8")


def save_facts_source(source: str) -> None:
    validate_module_source(source, "WORLD_FACTS")
    formatted = format_source(source)
    backup_file(FACTS_PATH)
    FACTS_PATH.write_text(formatted, encoding="utf-8")
