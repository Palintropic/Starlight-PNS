# pns/interfaces/redaction.py — 日志里的凭据遮蔽
#
# 为什么是包在流上，而不是一个 logging 过滤器：泄露最可能发生的地方不是我们
# 自己写的那几行日志，而是**异常路径**——一条被打印出来的 traceback、一个
# 第三方 SDK 在报错里回显的请求头、uvicorn 自己的堆栈。那些东西不经过我们的
# logger，但它们全都要从 stdout / stderr 出去。所以闸设在流上。
#
# 遮蔽的判据是**值**，不是变量名：从环境变量里现取当前值，在每一行输出里做
# 字面替换。取值放在写的时候而不是安装的时候，是为了让一次配置重载换掉的
# API Key 也照样被盖住。
#
# 两条它做不到的事，写在这里而不是假装没有：
#
#   * 太短的值不遮蔽（`MIN_REDACTABLE_CHARS`）。把一个 3 个字符的"密钥"从
#     所有输出里抹掉，会把日志本身毁掉，那不是安全，那是噪音。
#   * 一个恰好被 flush 切成两半的密钥，两半都不构成完整匹配，会漏。缓冲是
#     按行的，所以正常的日志行不会遇到这件事。
import io
import os
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

# 比这短的值不参与遮蔽：见上面的说明。
MIN_REDACTABLE_CHARS = 8

MASK = "***REDACTED***"


class SecretRedactor:
    """按环境变量名持有一组"当前值应当被遮蔽"的凭据。"""

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
        values = []
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


class RedactingStream(io.TextIOBase):
    """一个按行遮蔽的文本流包装。

    按行缓冲而不是逐次 write 处理：日志实现常常把一行拆成好几次 write
    （消息、分隔符、换行各一次），逐次处理会让跨 write 的密钥漏过去。
    """

    def __init__(self, stream, redactor: SecretRedactor) -> None:
        self._stream = stream
        self._redactor = redactor
        self._buffer = ""

    # ── 委托 ────────────────────────────────────────────────────────────
    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:  # pragma: no cover - 某些流没有 isatty
            return False

    def fileno(self) -> int:
        return self._stream.fileno()

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return getattr(self._stream, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._stream, "errors", None)

    @property
    def wrapped(self):
        return self._stream

    # ── 写 ──────────────────────────────────────────────────────────────
    def write(self, text) -> int:
        if not isinstance(text, str):  # pragma: no cover - 文本流只收 str
            text = str(text)
        self._buffer += text
        if "\n" in self._buffer:
            head, _, self._buffer = self._buffer.rpartition("\n")
            self._stream.write(self._redactor.apply(head + "\n"))
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            # 半行也要落地：flush 的意思就是"现在就要看到它"。遮蔽照做。
            self._stream.write(self._redactor.apply(self._buffer))
            self._buffer = ""
        self._stream.flush()


def install(
    env_names: Iterable[str], *, streams: Optional[Sequence[str]] = None
) -> SecretRedactor:
    """把 stdout / stderr 换成会遮蔽的版本。返回那个 redactor。

    幂等：已经装过的流不会被再包一层（否则每装一次就多一层缓冲）。
    """
    redactor = SecretRedactor(list(env_names))
    for name in streams if streams is not None else ("stdout", "stderr"):
        current = getattr(sys, name, None)
        if current is None or isinstance(current, RedactingStream):
            continue
        setattr(sys, name, RedactingStream(current, redactor))
    return redactor


def uninstall(streams: Optional[Sequence[str]] = None) -> None:
    """还原。只在测试里用得上。"""
    for name in streams if streams is not None else ("stdout", "stderr"):
        current = getattr(sys, name, None)
        if isinstance(current, RedactingStream):
            current.flush()
            setattr(sys, name, current.wrapped)


__all__ = [
    "MASK",
    "MIN_REDACTABLE_CHARS",
    "RedactingStream",
    "SecretRedactor",
    "install",
    "uninstall",
]
