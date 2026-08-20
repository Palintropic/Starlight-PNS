# pns/models/frozen.py — JSON 安全值的深冻结工具
#
# 事件、曝光决策、观察投影都要把外部传进来的字典变成只读视图：任何别的对象
# 都可能是调用方还持有引用的可变结构，放进不可变记录里就等于开了个后门。
#
# 这段逻辑原本只长在 pns/models/event.py 里。P6 又有两处需要同样的保证，
# 与其抄三份，不如抽出来一份 —— 它是安全相关的代码，不该有多个版本。
from types import MappingProxyType
from typing import Mapping, Sequence

# 只允许 JSON 安全的标量；其它类型一律拒绝，而不是"尽力序列化"。
_SCALARS = (str, int, float, bool, type(None))


def freeze_json_value(value, *, path: str = "value", error=ValueError):
    """把嵌套结构深冻结成只读视图；遇到不安全的值抛 error。

    error 由调用方给：这样各领域模型仍然抛自己的错误类型，调用方的
    except 不必知道这个工具模块的存在。
    """
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise error(f"{path} 的键必须是字符串，收到 {key!r}")
            frozen[key] = freeze_json_value(item, path=f"{path}.{key}", error=error)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(
            freeze_json_value(item, path=f"{path}[{index}]", error=error)
            for index, item in enumerate(value)
        )
    raise error(
        f"{path} 只能包含 JSON 安全的值（字符串/数字/布尔/None/字典/列表），"
        f"收到 {type(value).__name__}"
    )


def thaw_json_value(value):
    """把冻结视图还原成普通可变结构，供序列化与外部使用。"""
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value
