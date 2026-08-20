# pns/runtime/content_registry.py — 配置的单一构建入口
#
# 这个模块回答一个问题：**磁盘上的哪些东西算"配置"，它们怎么变成一个可以被
# 整体替换的对象。**
#
# 分类（完整说明见 docs/ARCHITECTURE.md「Configuration reload boundary」）：
#
#   1. Reloadable configuration —— 纯数据文件，不需要 import 就能读进来：
#      pns/world/scenes.py 的 SCENES/DEFAULT_SCENE 字面量、pns/world/facts.py 的
#      WORLD_FACTS 字面量、config.yaml、.env、packs/<pack>/ 下的 YAML 与提示词。
#      这些由 build_content_registry() 从磁盘重新读取、重新校验、重新构建。
#
#   2. Cold update —— Python 代码、领域模型、schema、运行算法，包括
#      pns/world/locations.py 与 channels.py 里那些要调用构造函数的结构定义。
#      改这些必须完全停服、替换文件、重启进程。本模块绝不 importlib.reload。
#
#   3. Runtime authoritative state —— 世界时间、位置、频道成员、事件、观察、
#      关系与记忆。ContentRegistry 里没有任何一个字段承载它们，也不提供任何
#      写入它们的方法；它们只能走 WorldState / Event 的运行时权威边界。
#
# ContentRegistry 是一份不可变快照。会话在 create() 时抓住一份，整个生命周期里
# 都用同一份 —— 所以重载切换全局引用时，正在跑的会话不会读到撕裂的半新半旧配置。
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple

import yaml
from dotenv import dotenv_values, load_dotenv

from pns.models.channel import ChannelRegistry
from pns.models.frozen import freeze_json_value, thaw_json_value
from pns.models.location import LocationGraph
from pns.world.channels import build_default_channel_registry
from pns.world.data_module import DataModuleError, evaluate_data_source, require
from pns.world.characters.registry import CharacterNotReadyError, load_pack_data
from pns.world.context import render_world_context
from pns.world.locations import build_default_location_graph
from pns.world.scene_compat import (
    SCENE_WORLD_MAP,
    SceneMappingError,
    _parse_time,
    build_initial_world_state,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENES_PATH = REPO_ROOT / "pns" / "world" / "scenes.py"
FACTS_PATH = REPO_ROOT / "pns" / "world" / "facts.py"
CONFIG_PATH = REPO_ROOT / "config.yaml"
ENV_PATH = REPO_ROOT / ".env"

VALID_API_FORMATS = ("anthropic", "openai")

# 供文档与测试引用的分类清单。改动这三张表就是在改重载边界本身，
# 必须同时改 docs/ARCHITECTURE.md 和 tests/test_config_reload.py。
RELOADABLE_SOURCES: Tuple[str, ...] = (
    "pns/world/scenes.py (SCENES / DEFAULT_SCENE 字面量)",
    "pns/world/facts.py (WORLD_FACTS 字面量)",
    "config.yaml",
    ".env",
    "packs/<active_pack>/**",
)
COLD_UPDATE_SOURCES: Tuple[str, ...] = (
    "pns/**/*.py (任何需要 import 才能生效的代码)",
    "pns/world/locations.py / channels.py (结构定义，需要构造函数)",
    "pns/world/scene_compat.py 的 SCENE_WORLD_MAP",
    "pns/models/** (领域模型与 schema)",
    "pns/runtime/exposure/** (运行算法)",
    "requirements.txt / 依赖版本",
)
RUNTIME_AUTHORITATIVE_STATE: Tuple[str, ...] = (
    "WorldState.clock (世界时间)",
    "WorldState.character_locations (位置)",
    "WorldState.channel_members (频道成员)",
    "WorldState.availability",
    "WorldState.environment",
    "SessionState.events / EventStore (事件)",
    "SessionState.observations (观察)",
    "关系与记忆（尚未实现，但同样不走配置）",
)


class ConfigValidationError(ValueError):
    """磁盘上的配置读不出来或者过不了校验。抛出它意味着这次重载整体失败。"""


# ── 数据文件读取 ──────────────────────────────────────────────────────────


def _read_data_module(path: Path) -> Dict:
    """把一个纯数据 .py 文件求值成命名空间。

    走 pns/world/data_module.py 的严格 AST 白名单：源码不会被执行，只有字面量
    赋值能通过。之所以不用 importlib，是因为 import 缓存与 reload 会让
    「半新半旧」成为可能，而这里要的是「读一份新的出来，读失败就整体作废」。
    """
    if not path.exists():
        raise ConfigValidationError(f"配置文件不存在：{path}")
    try:
        return evaluate_data_source(path.read_text(encoding="utf-8"), path.name)
    except DataModuleError as e:
        raise ConfigValidationError(str(e)) from e


def _require(namespace: Dict, name: str, expected_type, path: Path):
    try:
        return require(namespace, name, expected_type, path.name)
    except DataModuleError as e:
        raise ConfigValidationError(str(e)) from e


def _freeze(value, label: str):
    """深冻结一份 JSON 数据；不是 JSON 安全的值直接判这次配置不合格。"""
    return freeze_json_value(value, path=label, error=ConfigValidationError)


def _load_settings(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigValidationError(f"config.yaml 解析失败：{e}") from e
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigValidationError(
            f"config.yaml 顶层必须是映射，实际是 {type(loaded).__name__}"
        )
    return loaded


# ── 模型 / provider 配置 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelSettings:
    """.env 里那套 provider/模型设定的一份快照。

    API Key 本身不进快照（不想让密钥在内存里到处复制），只记住它的变量名，
    需要时现从 os.environ 取。
    """

    provider: str
    api_format: str
    base_url: str
    key_name: str
    model: str
    generator_model: str
    evaluator_model: str
    ooc_threshold: float

    @classmethod
    def from_env(cls, env: Mapping = None) -> "ModelSettings":
        env = os.environ if env is None else env
        api_format = env.get("API_FORMAT", "anthropic")
        if api_format not in VALID_API_FORMATS:
            raise ConfigValidationError(
                f"API_FORMAT 必须是 {'/'.join(VALID_API_FORMATS)} 之一，实际是 {api_format!r}"
            )
        base_url = env.get("BASE_URL", "https://api.xiaomimimo.com/anthropic")
        if not base_url:
            raise ConfigValidationError("BASE_URL 不能为空")
        key_name = env.get("PNS_API_KEY_NAME", "MIMO_API_KEY")
        if not key_name:
            raise ConfigValidationError("PNS_API_KEY_NAME 不能为空")
        raw_threshold = env.get("OOC_THRESHOLD", "5")
        try:
            ooc_threshold = float(raw_threshold)
        except (TypeError, ValueError) as e:
            raise ConfigValidationError(
                f"OOC_THRESHOLD 必须是数字，实际是 {raw_threshold!r}"
            ) from e
        if not 0 <= ooc_threshold <= 10:
            raise ConfigValidationError(
                f"OOC_THRESHOLD 必须落在 0-10，实际是 {ooc_threshold}"
            )
        model = env.get("MODEL", "mimo-v2.5-pro")
        return cls(
            provider=env.get("PROVIDER", ""),
            api_format=api_format,
            base_url=base_url,
            key_name=key_name,
            model=model,
            generator_model=env.get("GENERATOR_MODEL") or model,
            evaluator_model=env.get("EVALUATOR_MODEL") or model,
            ooc_threshold=ooc_threshold,
        )

    @property
    def api_key(self) -> str:
        return os.environ.get(self.key_name, "")

    def to_dict(self) -> Dict:
        return {
            "provider": self.provider,
            "api_format": self.api_format,
            "base_url": self.base_url,
            "key_name": self.key_name,
            "model": self.model,
            "generator_model": self.generator_model,
            "evaluator_model": self.evaluator_model,
            "ooc_threshold": self.ooc_threshold,
        }


# ── 角色内容 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CharacterContent:
    """一个角色在这份快照里的全部文本内容，构建时一次性读进内存。

    构建期就把提示词读出来，是为了让"配置有问题"在切换之前暴露，而不是等到
    某个角色第一次开口时才 500。
    """

    character_id: str
    metadata: Mapping
    system_prompt: Optional[str]
    prompt_error: Optional[str]
    compat_prompt: Optional[str]
    constitution: Optional[str]
    router_reference: Optional[str]

    @property
    def status(self) -> str:
        return str(self.metadata.get("status", ""))

    @property
    def name(self) -> str:
        return str(self.metadata.get("name", self.character_id))


def _read_optional(base_dir: Path, filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    path = base_dir / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


def _build_character(character_id: str, info: Dict, pack_dir: Path) -> CharacterContent:
    char_dir = pack_dir / "characters" / info["unit"] / character_id

    system_prompt: Optional[str] = None
    prompt_error: Optional[str] = None
    prompt_file = info.get("prompt_file")
    if not prompt_file:
        prompt_error = f"角色 {character_id} 未声明 prompt_file"
    else:
        prompt_path = char_dir / prompt_file
        if prompt_path.exists():
            system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        else:
            prompt_error = (
                f"角色 {character_id}（status={info.get('status')}）尚无 system prompt，"
                f"预期路径 {prompt_path}"
            )

    return CharacterContent(
        character_id=character_id,
        # 角色元数据里有 list（别名、标签等），浅冻结挡不住 append —— 深冻。
        metadata=freeze_json_value(
            info, path=f"角色 {character_id} 的元数据", error=ConfigValidationError
        ),
        system_prompt=system_prompt,
        prompt_error=prompt_error,
        compat_prompt=_read_optional(char_dir, info.get("prompt_file_compat")),
        constitution=_read_optional(char_dir, info.get("constitution_file")),
        router_reference=_read_optional(char_dir, info.get("router_reference_file")),
    )


# ── 注册表本体 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContentRegistry:
    """一次构建产出的完整内容快照。

    构建成功之后是**深度**只读的：scenes / world_facts / settings / 角色元数据
    全部走 freeze_json_value 递归冻结，嵌套的 dict 变只读视图、list 变 tuple。
    浅冻结不够 —— `scenes["gate"]["gate_triggers"]["A"] = ...` 这种改法能绕过
    一层 MappingProxyType，让一份本该稳定的快照在会话跑到一半时变形。
    """

    revision: int
    built_at: str
    pack_name: str
    scenes: Mapping[str, Mapping]
    default_scene: str
    world_facts: Mapping[str, str]
    characters: Mapping[str, CharacterContent]
    settings: Mapping
    models: ModelSettings

    # ── 场景 ──────────────────────────────────────────────────────────
    #
    # 注册表内部存的是深冻结视图；对外一律交解冻副本。调用方拿到的东西改坏了
    # 也只是改坏自己那份，动不到这份快照 —— 会话可以放心地把 scene 当成自己的
    # 局部数据用，而不必约定"看看就好别改"。
    def scene(self, scene_id: Optional[str]) -> Dict:
        """取场景的可变副本；未知 id 回落到默认场景（与重构前的行为一致）。"""
        if scene_id in self.scenes:
            return thaw_json_value(self.scenes[scene_id])
        return thaw_json_value(self.scenes[self.default_scene])

    def scenes_snapshot(self) -> Dict:
        """全部场景的可变副本（World Editor 的 GET 用这个）。"""
        return thaw_json_value(self.scenes)

    def facts_snapshot(self) -> Dict:
        return thaw_json_value(self.world_facts)

    def settings_snapshot(self) -> Dict:
        return thaw_json_value(self.settings)

    # ── 角色 ──────────────────────────────────────────────────────────
    def has_character(self, character_id: str) -> bool:
        return character_id in self.characters

    def character(self, character_id: str) -> CharacterContent:
        try:
            return self.characters[character_id]
        except KeyError:
            raise ValueError(f"Character not found in registry: {character_id}") from None

    def character_metadata(self, character_id: str) -> Dict:
        return thaw_json_value(self.character(character_id).metadata)

    def character_name(self, character_id: str) -> str:
        return self.character(character_id).name

    def router_reference(self, character_id: str) -> Optional[str]:
        return self.character(character_id).router_reference

    def character_system(self, character_id: str, context, compat: bool = False) -> str:
        """组装角色 system prompt。等价于 pns.world.get_character_system，
        但文本全部来自本快照，运行期不再回磁盘 —— 会话中途改文件不会串味。"""
        content = self.character(character_id)
        prompt = content.compat_prompt if compat else None
        if prompt is None:
            prompt = content.system_prompt
        if prompt is None:
            raise CharacterNotReadyError(
                character_id, content.prompt_error or f"角色 {character_id} 没有可用的提示词"
            )

        world_state = (
            render_world_context(context, character_id)
            if hasattr(context, "clock")
            else _legacy_scene_context(context)
        )
        system_prompt = prompt.format(world_state=world_state)
        if content.constitution is not None:
            system_prompt = (
                f"【角色宪法：生成回复后用于自我检查】\n{content.constitution}"
                f"\n\n【角色事实、当前场景与输出要求】\n{system_prompt}"
            )
        return system_prompt

    # ── 结构（cold code，但每次都重新构造以保证会话之间不共享可变对象）──
    def new_location_graph(self) -> LocationGraph:
        return build_default_location_graph()

    def new_channel_registry(self) -> ChannelRegistry:
        return build_default_channel_registry()

    def new_world_state(self, scene: Mapping, character_ids):
        """把场景投影成一份**新的**权威 WorldState。

        注意方向：配置 → 初始状态，只发生一次，且只发生在会话开始时。
        没有任何反向通道能让重载去改一个已经存在的 WorldState。
        """
        return build_initial_world_state(
            scene,
            character_ids,
            locations=self.new_location_graph(),
            channels=self.new_channel_registry(),
        )

    def to_dict(self) -> Dict:
        return {
            "revision": self.revision,
            "built_at": self.built_at,
            "pack": self.pack_name,
            "scene_count": len(self.scenes),
            "default_scene": self.default_scene,
            "fact_count": len(self.world_facts),
            "character_count": len(self.characters),
            "ready_characters": sorted(
                cid for cid, c in self.characters.items() if c.status == "ready"
            ),
            "models": self.models.to_dict(),
        }


def _legacy_scene_context(scene) -> str:
    """遗留 scene dict 的世界上下文渲染（兼容路径，与 pns.world 保持一致）。"""
    return (
        f"时间：{scene['time']}，"
        f"地点：{scene['location']}，"
        f"天气/环境：{scene['weather']}"
    )


# ── 构建 ──────────────────────────────────────────────────────────────────


def _validate_scenes(scenes: Dict, default_scene: str, locations, channels) -> None:
    if not scenes:
        raise ConfigValidationError("SCENES 不能为空")
    for scene_id, scene in scenes.items():
        if not isinstance(scene, dict):
            raise ConfigValidationError(
                f"场景 '{scene_id}' 必须是 dict，实际是 {type(scene).__name__}"
            )
        if scene.get("id") != scene_id:
            raise ConfigValidationError(
                f"场景 key '{scene_id}' 与内部 id '{scene.get('id')}' 不一致"
            )
        for required in ("label", "time", "location", "weather", "trigger"):
            if not isinstance(scene.get(required), str):
                raise ConfigValidationError(
                    f"场景 '{scene_id}' 缺少字符串字段 '{required}'"
                )
        try:
            _parse_time(scene.get("time"))
        except SceneMappingError as e:
            raise ConfigValidationError(f"场景 '{scene_id}' 时间不合法：{e}") from e

        mapping = SCENE_WORLD_MAP.get(scene_id)
        if mapping is None:
            raise ConfigValidationError(
                f"场景 '{scene_id}' 在 pns/world/scene_compat.py 的 SCENE_WORLD_MAP 里"
                f"没有世界映射。补映射属于 cold update（改代码后重启），"
                f"不能只靠重新加载配置。"
            )
        for location_id in (
            mapping.default_location_id,
            *mapping.character_locations.values(),
        ):
            if not locations.has(location_id):
                raise ConfigValidationError(
                    f"场景 '{scene_id}' 映射到未知地点 '{location_id}'"
                )
        for channel_id in mapping.channel_ids:
            if not channels.has(channel_id):
                raise ConfigValidationError(
                    f"场景 '{scene_id}' 映射到未知频道 '{channel_id}'"
                )

        auto_next = scene.get("auto_next")
        if auto_next is not None and auto_next not in scenes:
            raise ConfigValidationError(
                f"场景 '{scene_id}' 的 auto_next 指向不存在的场景 '{auto_next}'"
            )

    if default_scene not in scenes:
        raise ConfigValidationError(
            f"DEFAULT_SCENE '{default_scene}' 不在 SCENES 里"
        )


def build_content_registry(revision: int = 0) -> ContentRegistry:
    """从磁盘完整构建一份新的内容注册表。

    这是**唯一**的配置构建入口。任何一步失败都抛 ConfigValidationError，
    调用方（ConfigBoundary）据此决定继续用旧快照。

    失败路径上这个函数不留任何痕迹：它不改全局引用、不碰运行时权威状态，
    也不动 os.environ —— .env 是先读出来参与校验，等全部通过之后才落到
    进程环境里。否则一次失败的重载会把新的 API Key 塞进环境，让"仍在使用
    上一份可用配置"变成一句假话。
    """
    env_file = {
        k: v for k, v in dotenv_values(ENV_PATH).items() if v is not None
    } if ENV_PATH.exists() else {}
    # 与 load_dotenv(override=True) 同样的优先级：.env 覆盖进程环境。
    models = ModelSettings.from_env({**os.environ, **env_file})
    settings = _load_settings(CONFIG_PATH)

    scenes_ns = _read_data_module(SCENES_PATH)
    scenes = _require(scenes_ns, "SCENES", dict, SCENES_PATH)
    default_scene = _require(scenes_ns, "DEFAULT_SCENE", str, SCENES_PATH)

    facts_ns = _read_data_module(FACTS_PATH)
    world_facts = _require(facts_ns, "WORLD_FACTS", dict, FACTS_PATH)

    # 结构对象属于 cold code，但每次构建都重新构造一遍：构造函数自带引用完整性
    # 校验，等于顺手确认了"当前代码 + 当前配置"这个组合是自洽的。
    try:
        locations = build_default_location_graph()
        channels = build_default_channel_registry()
    except ValueError as e:
        raise ConfigValidationError(f"位置图/频道表构建失败：{e}") from e

    _validate_scenes(scenes, default_scene, locations, channels)

    try:
        pack = load_pack_data()
    except ValueError as e:
        raise ConfigValidationError(f"角色包加载失败：{e}") from e

    characters: Dict[str, CharacterContent] = {}
    for character_id, info in pack["characters"].items():
        content = _build_character(character_id, info, pack["pack_dir"])
        # status=ready 的角色必须真的能开口，否则整份配置不算通过。
        if content.status == "ready" and content.system_prompt is None:
            raise ConfigValidationError(content.prompt_error or f"角色 {character_id} 无提示词")
        characters[character_id] = content

    # 全部校验通过，这一份配置确定可用了，才把 .env 落到进程环境里。
    if env_file:
        load_dotenv(ENV_PATH, override=True)

    return ContentRegistry(
        revision=revision,
        built_at=datetime.now().isoformat(timespec="seconds"),
        pack_name=pack["pack_name"],
        scenes=_freeze(scenes, "SCENES"),
        default_scene=default_scene,
        world_facts=_freeze(world_facts, "WORLD_FACTS"),
        characters=MappingProxyType(characters),
        settings=_freeze(settings, "config.yaml"),
        models=models,
    )
