# pns/interfaces/redaction.py — 日志里的凭据遮蔽
#
# 为什么是包在流上，而不是一个 logging 过滤器：泄露最可能发生的地方不是我们
# 自己写的那几行日志，而是**异常路径**——一条被打印出来的 traceback、一个
# 第三方 SDK 在报错里回显的请求头、uvicorn 自己的堆栈。那些东西不经过我们的
# logger，但它们全都要从 stdout / stderr 出去。所以闸设在流上。
#
# 遮蔽的判据是**值**，不是变量名：从环境变量里现取当前值，在每一行输出里做
# 字面替换，并合并设置文件登记的卡片 token（含旧值）。取值放在写的时候
# 而不是安装的时候，是为了让配置重载换掉的 API Key 也照样被盖住。
#
# 两条它做不到的事，写在这里而不是假装没有：
#
#   * 太短的值不遮蔽（`MIN_REDACTABLE_CHARS`）。把一个 3 个字符的"密钥"从
#     所有输出里抹掉，会把日志本身毁掉，那不是安全，那是噪音。
#   * 一个恰好被 flush 切成两半的密钥，两半都不构成完整匹配，会漏。缓冲是
#     按行的，所以正常的日志行不会遇到这件事。
import io
import logging
from urllib.parse import unquote
import sys
from typing import Iterable, Optional, Sequence

from pns.runtime import secret_values
from pns.runtime.secret_values import MASK, MIN_REDACTABLE_CHARS, SecretRedactor


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
    secret_values.process_redactor = redactor
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
    "SsoAccessLogFilter",
    "install",
    "uninstall",
]


class SsoAccessLogFilter(logging.Filter):
    """Uvicorn's access tuple: client, method, raw target, version, status."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            target = record.args[2]
            if isinstance(target, str):
                path = target.split("?", 1)[0]
                if unquote(path).startswith("/api/auth/sso/"):
                    record.args = (*record.args[:2], path, *record.args[3:])
        return True
