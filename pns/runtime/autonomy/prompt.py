# pns/runtime/autonomy/prompt.py — 把角色作用域的生成上下文渲染成一次模型调用
#
# 这一层回答的问题只有一个：**GenerationContext 里那些东西，怎么变成交给模型
# 的那两段文本（角色 system prompt + 这一刻的情境）。**
#
# 它不回答：什么时候该考虑（Scheduler）、要不要动（Agency）、说得像不像本人
# （Router）、记不记得住（Memory），也不回答"用哪个 provider、拿哪把 key 打
# 哪个 endpoint" —— 那是接线层的事，在 pns/interfaces/composition.py。
#
# 四条硬约束：
#
#   1. **只从 GenerationContext 取数。** 这个模块拿不到 SessionState、拿不到
#      WorldState、拿不到 EventStore、拿不到曝光判定日志，也拿不到别人的观察
#      与记忆 —— 它的输入就只有那一份已经收窄过的上下文，加上**冻结的内容包**
#      里那些静态显示名。所以"这份提示词里会不会出现它不该知道的事"这个问题，
#      答案完整地写在 GenerationContext 的字段表上。
#   2. **世界投影是角色作用域的，而且它自己就是那道闸。** CharacterWorldView
#      只回答"我在哪、我挂着哪些频道"；问它别人在哪，它抛错，而不是回答。
#      交一份完整 WorldState 给提示词渲染层也能跑通（渲染层只读这个角色的
#      那一份），但那样"没漏"就只是一次观察，而不是一个机制。
#   3. **模型调用是注入进来的可调用对象。** 这个模块不 import 任何模型 SDK，
#      也不知道 API Key 长什么样（有测试盯着整个 autonomy 包）。确定性适配器
#      （ScriptedLineGenerator）和真实适配器走的是同一条解析与校验通道。
#   4. **provider 那侧的任何东西都不许穿过这层边界。** 调用失败时对外的消息
#      是**完全固定**的：不含 str(e)、repr(e)、类型名，也不含异常的任何属性。
#      理由跟 composition.py 里那段一样 —— 类型名装得下一把 key
#      （`type(api_key, (RuntimeError,), {})`），所以只要还有任何一处从异常
#      派生的数据能过边界，这条边界就还是漏的。而这里比别处更要紧：策略失败
#      的消息会被 Agency 引擎原样写进 detail，跟着存档一起落盘、再从 API 交
#      出去。原始异常留在 __cause__ 里，谁在服务器侧调试谁自己去看。
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from pns.models.action import ActionId
from pns.runtime.autonomy.context import GenerationContext
from pns.runtime.autonomy.generation import GenerationError, LineGenerator
from pns.world.context import render_world_context

# 对外那句固定的失败原文。它们是**常量**，不是模板 —— 没有任何 provider 侧
# 的数据能拼进来。
PROVIDER_FAILURE = "生成模型调用失败；请检查服务器侧的 provider 与凭据配置"
PROMPT_FAILURE = "角色提示词渲染失败；请检查内容包里这个角色的提示词模板"

# 交给模型的情境里，最多列几条观察 / 几条回忆。上下文预算，不是审美：
# 一份无限长的情境会把一次生成的成本变成世界活跃度的函数。
MAX_OBSERVED_LINES = 12
MAX_RECALLED_LINES = 8


class _Named:
    """一个只有显示名的最小条目。查不到内容包条目时的兜底形状。"""

    __slots__ = ("display", "name")

    def __init__(self, entity_id: str):
        self.display = entity_id
        self.name = entity_id


class _Displays:
    """内容包里某张表的只读显示名查询，**查不到就用 id 本身**。

    兜底而不是抛错，是因为显示名是装饰：一个查不到的地点 id 不该让整次生成
    失败，更不该让这个角色因此永远说不出话。真正要响亮失败的是作用域越界，
    那是另一回事，由 CharacterWorldView 管。
    """

    __slots__ = ("_registry",)

    def __init__(self, registry=None):
        self._registry = registry

    def get(self, entity_id: str):
        if self._registry is not None:
            try:
                return self._registry.get(entity_id)
            except Exception:
                pass
        return _Named(entity_id)


class PromptScopeError(RuntimeError):
    """有人向一份角色作用域的世界投影，问了一个越界的问题。

    它不是"查不到"，是"这里根本不该有这个答案"。所以它响亮失败，不返回空值 ——
    返回空值会让一次越界读取看起来像一次正常的缺失。
    """


@dataclass(frozen=True)
class CharacterWorldView:
    """一个角色此刻的世界，长成提示词渲染层（pns/world/context.py）认得的样子。

    它是 WorldState 的**替身**，不是它的子集视图：字段全部来自
    GenerationContext，所以它物理上不持有别人的位置、别人的频道、事件历史或
    曝光判定。地点与频道的**显示名**来自冻结的内容包 —— 那是静态内容，不是
    运行时权威状态，谁都可以读。

    `environment_of()` 恒返回空：环境（天气）是 WorldState 上的运行时状态，
    GenerationContext 没有携带它。宁可让提示词少一句天气，也不为了这一句
    把整份世界状态递进生成层。
    """

    character_id: str
    clock: datetime
    location_id: Optional[str] = None
    channel_ids: Tuple[str, ...] = ()
    # 冻结内容包里的地点表与频道表（**只**用来查显示名）。默认是查不到就
    # 回落到 id 的兜底表，所以渲染层永远拿得到一个字符串，不会因为少一份
    # 内容表就炸在提示词渲染上。
    locations: object = field(default_factory=_Displays)
    channels: object = field(default_factory=_Displays)

    # ── 提示词渲染层会问的那几个问题 ────────────────────────────────────
    def location_of(self, character_id: str) -> Optional[str]:
        self._require_self(character_id, "位置")
        return self.location_id

    def channels_for(self, character_id: str) -> List[str]:
        self._require_self(character_id, "频道")
        return list(self.channel_ids)

    def availability_of(self, character_id: str):  # pragma: no cover - 渲染层不问
        self._require_self(character_id, "可用性")
        raise PromptScopeError("生成提示词不读可用性")

    def environment_of(self, location_id: str) -> dict:
        return {}

    # ── 全知的那几个问题：一律拒绝 ──────────────────────────────────────
    @property
    def character_locations(self):
        raise PromptScopeError(
            "角色作用域的世界投影里没有'所有人在哪' —— 那是全知数据"
        )

    @property
    def channel_members(self):
        raise PromptScopeError(
            "角色作用域的世界投影里没有'每个频道有谁' —— 那是全知数据"
        )

    def known_characters(self):
        raise PromptScopeError(
            "角色作用域的世界投影里没有'世界上有谁' —— 那是全知数据"
        )

    def _require_self(self, character_id: str, what: str) -> None:
        if character_id != self.character_id:
            raise PromptScopeError(
                f"'{self.character_id}' 的生成上下文答不了 '{character_id}' 的{what}"
            )


def build_world_view(
    context: GenerationContext, *, locations=None, channels=None
) -> CharacterWorldView:
    """GenerationContext → 提示词渲染层要的那份角色作用域投影。

    刻意只做转写，不做取数：每个字段都来自传进来的那份上下文。
    """
    if not isinstance(context, GenerationContext):
        raise GenerationError("build_world_view() 需要一个 GenerationContext")
    return CharacterWorldView(
        character_id=context.character_id,
        clock=context.now,
        location_id=context.location_id,
        channel_ids=tuple(context.channel_ids),
        locations=_Displays(locations),
        channels=_Displays(channels),
    )


def _display(registry, entity_id: str) -> str:
    """内容包里那个 id 的显示名；查不到就用 id 本身，不猜。"""
    if not entity_id:
        return ""
    return _Displays(registry).get(entity_id).display


def _action_line(context: GenerationContext, channels) -> str:
    """这一刻要做的那个动作，说成角色听得懂的一句话。

    动作是 Agency 选定的，不是模型选的 —— 所以这里是陈述句，不是选择题。
    """
    if context.action_id is ActionId.SEND_CHANNEL_MESSAGE:
        return f"你现在要在「{_display(channels, context.target_id)}」里发一条消息。"
    if context.action_id is ActionId.SPEAK_HERE:
        return "你现在要在你所在的地方开口说一句话。"
    # 目录以后会长出别的需要台词的动作。到那时这里要补一条它自己的说法，
    # 而不是让它悄悄套用上面某一条。
    return f"你现在要做的事：{context.action_id.value}。"


def render_situation(
    context: GenerationContext,
    *,
    view: Optional[CharacterWorldView] = None,
    channels=None,
    names: Optional[Mapping[str, str]] = None,
    max_observed: int = MAX_OBSERVED_LINES,
    max_recalled: int = MAX_RECALLED_LINES,
) -> str:
    """把这一刻交给模型的情境渲染成一段文本。

    每一段都能指回 GenerationContext 上的一个字段（`view` 也是从它转写出来
    的）。没有出现在那张字段表上的东西，这里一个字也变不出来。

    时间/地点这一段刻意在**这里**渲染，而不是指望角色 system prompt 里那个
    `{world_state}` 占位符：内容包里的提示词模板不保证有它（现役的两份就
    没有），于是"世界上下文进没进提示词"会变成一件靠内容作者记得填占位符的
    事。它太重要了，不能靠这个。
    """
    if not isinstance(context, GenerationContext):
        raise GenerationError("render_situation() 需要一个 GenerationContext")
    if view is None:
        view = build_world_view(context, channels=channels)

    parts: List[str] = [
        # 只读这个角色自己的位置与频道 —— 问它别人在哪，这份投影会抛错。
        "【此刻】" + render_world_context(view, context.character_id)
    ]

    if context.perceived_characters:
        # 显示名来自**冻结的内容包**，不来自模型输出，也不来自世界状态。
        # 查不到就用 id 本身 —— 不猜，也不因为一个名字查不到就整次生成失败。
        parts.append(
            "【此刻和你在一起的】"
            + "、".join(
                (names or {}).get(cid, cid) for cid in context.perceived_characters
            )
        )

    observed = _tail(context.observed_lines, max_observed)
    if observed:
        parts.append("【你刚刚看到/听到的】\n" + "\n".join(f"- {line}" for line in observed))
    elif context.observations:
        # 有观察、但一条都渲染不成对话行（比如只观察到别人上线/离线）。
        # 说清楚"你什么都没听见"和"这里没写"是两回事。
        parts.append("【你刚刚看到/听到的】\n- （没有人说话）")

    recalled = _tail(context.recalled, max_recalled)
    if recalled:
        parts.append("【你现在想起的】\n" + "\n".join(f"- {line}" for line in recalled))

    cue = context.activation.cue
    if cue:
        # 内容作者显式声明为角色可见的那一句。排期簿记的其余部分在
        # ActivationCue.from_due() 那道白名单就已经被挡在外面了。
        parts.append(f"【此刻你心里的事】{cue}")

    parts.append(_action_line(context, channels))
    parts.append(
        "只输出你这一刻要说的那一句话本身：不要旁白、不要动作描写、"
        "不要加你自己的名字前缀，也不要解释你为什么这么说。"
    )
    return "\n\n".join(parts)


def _tail(lines: Sequence[str], limit: int) -> Tuple[str, ...]:
    if limit <= 0:
        return ()
    return tuple(lines[-limit:])


class PromptedLineGenerator(LineGenerator):
    """真实模型生成的适配器。

    构造只吃三样东西：一个可调用对象、以及内容包里的地点/频道显示名表。
    它拿不到 SessionState、拿不到世界、拿不到模型客户端，也拿不到凭据 ——
    所以它没有任何提交的能力，也没有任何泄漏凭据的路径。真实接线
    （客户端、模型名、provider 分支）在 pns/interfaces/composition.py。

    `call(character_id, view, history) -> str`：view 是角色作用域的世界投影，
    history 是一份 `[{"role": "user", "content": ...}]`。返回值是**不可信
    输入**，交回给 parse_line() 与 Agency 的校验通道去判。
    """

    name = "prompted"

    def __init__(
        self,
        call: Callable[[str, CharacterWorldView, List[dict]], object],
        *,
        locations=None,
        channels=None,
        names: Optional[Mapping[str, str]] = None,
        name: Optional[str] = None,
    ):
        if not callable(call):
            raise GenerationError("生成适配器需要一个可调用的模型调用")
        if names is not None and not isinstance(names, Mapping):
            raise GenerationError("names 必须是字典")
        self._call = call
        self._locations = locations
        self._channels = channels
        self._names = dict(names or {})
        if name is not None:
            if not isinstance(name, str) or not name:
                raise GenerationError("name 必须是非空字符串")
            self.name = name

    def generate(self, context: GenerationContext) -> object:
        try:
            view = build_world_view(
                context, locations=self._locations, channels=self._channels
            )
            situation = render_situation(
                context, view=view, channels=self._channels, names=self._names
            )
        except GenerationError:
            raise
        except Exception as e:
            # 渲染这一侧的失败是**不可重试**的：同一份上下文再渲染一百次也是
            # 同样的结果。消息固定 —— 模板片段和内容包内部结构不该跟着策略
            # 失败一起被写进存档。
            raise GenerationError(PROMPT_FAILURE, retryable=False) from e

        history = [{"role": "user", "content": situation}]
        try:
            return self._call(context.character_id, view, history)
        except GenerationError:
            # 接线层自己已经把这次失败翻译成了一句安全的话（比如"被 max_tokens
            # 截断"）。原样放行，不要再盖成一句更笼统的 —— 盖掉之后"半句话"
            # 和"连不上 provider"就分不开了。
            raise
        except PromptScopeError as e:
            # 有人向这份角色作用域的投影问了越界的问题（渲染层在真正发请求
            # 之前就会问）。这不是 provider 故障，也不会自己好起来：它是一次
            # 需要改代码的作用域违规，所以**不可重试**，而且要跟 provider
            # 故障区分开，免得被当成"网络抖了一下"重试三次。
            raise GenerationError(
                "生成提示词越界：有人向角色作用域的世界投影问了全知问题",
                retryable=False,
            ) from e
        except Exception as e:
            # provider 那侧什么都可能发生，而**它收到过 API Key**，所以从它
            # 出来的一切都是不可信数据 —— 异常本身也是。这句话完全固定。
            raise GenerationError(PROVIDER_FAILURE, retryable=True) from e


__all__ = [
    "MAX_OBSERVED_LINES",
    "MAX_RECALLED_LINES",
    "PROMPT_FAILURE",
    "PROVIDER_FAILURE",
    "CharacterWorldView",
    "PromptScopeError",
    "PromptedLineGenerator",
    "build_world_view",
    "render_situation",
]
