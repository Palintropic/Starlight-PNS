"""进程内凭据遮蔽核心；仅依赖标准库，不读取配置、不安装流、不导入应用。"""
import os
import threading
from collections import OrderedDict
from typing import Iterable, List, Sequence, Tuple

# 保留最近登记的 256 个不同值，包括已替换 / 删除的 token。超过上限时淘汰
# 最久没有再登记的值。每次读设置都会重新登记全部现役卡片，所以被淘汰的只会
# 是早已不在设置里的旧 token：以最久远的异常遮蔽窗口换取有界内存是可接受的。
MAX_SECRET_VALUES = 256
_values = OrderedDict()
_values_lock = threading.Lock()


def register_values(values: Iterable[str]) -> None:
    """只追加、不注销；再次登记的值移到最新，现役 token 不会被挤出。"""
    for value in values:
        value = value.strip()
        if len(value) < MIN_REDACTABLE_CHARS:
            continue
        with _values_lock:
            _values[value] = None
            _values.move_to_end(value)
            while len(_values) > MAX_SECRET_VALUES:
                _values.popitem(last=False)


def secret_values_snapshot() -> Tuple[str, ...]:
    """输出线程只遍历快照，不持锁替换日志文字。"""
    with _values_lock:
        return tuple(_values)


# 太短的值会过度遮蔽普通日志；环境变量和登记值使用同一阈值。
MIN_REDACTABLE_CHARS = 8

MASK = "***REDACTED***"


class SecretRedactor:
    """合并环境变量现值和进程内登记值，供流和状态出口共用。"""

    def __init__(self, env_names: Sequence[str], *, env=None) -> None:
        # 去重但保序：报错信息与测试都更好读。
        seen = []
        for name in env_names:
            if name and name not in seen:
                seen.append(name)
        self._names: Tuple[str, ...] = tuple(seen)
        self._env = env

    @property
    def names(self) -> Tuple[str, ...]:
        return self._names

    def secrets(self) -> List[str]:
        env = os.environ if self._env is None else self._env
        values = list(secret_values_snapshot())
        for name in self._names:
            value = env.get(name) or ""
            value = value.strip()
            if len(value) >= MIN_REDACTABLE_CHARS and value not in values:
                values.append(value)
        # 长的先替换：一个短密钥恰好是长密钥的前缀时，先换短的会留下尾巴。
        values.sort(key=len, reverse=True)
        return values

    def apply(self, text: str) -> str:
        if not text:
            return text
        for secret in self.secrets():
            if secret in text:
                text = text.replace(secret, MASK)
        return text


# 流安装时换成同一个 redactor，让非流出口也使用相同的环境变量名单。
# 未安装流时仍能遮蔽已从设置文档登记的值。
process_redactor = SecretRedactor(())
