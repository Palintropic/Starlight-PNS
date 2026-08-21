# pns/runtime/persistence/naming.py — world_id 的合法性
#
# world_id 是**不可信文本**：它会从 HTTP 请求、配置文件、以后的后台 UI 里进来，
# 然后变成一条会被 mkdir、被写、被 os.replace 的路径。所以它先在这里过一遍，
# 而且过不去就没有任何路径会被拼出来。
#
# 规则刻意收得很紧，收紧的每一条都对应一种真实的事故：
#
#   * 只允许小写 ASCII 字母、数字和 `.` `_` `-`。macOS / Windows 的文件系统
#     大小写不敏感，`Nightcord` 和 `nightcord` 会落在同一个目录里 —— 于是
#     "同一个世界只能有一个拥有者"这条保证会被两个看起来不同的 ID 绕过去。
#     解决办法不是"归一化成小写"（那等于悄悄把两个 ID 合成一个），是拒绝。
#   * 只允许 ASCII。`café` 有 NFC / NFD 两种写法，字节不同、在磁盘上却是同一个
#     目录，跟大小写是同一种事故。
#   * 首字符必须是字母或数字，尾字符不能是 `.` `_` `-`。这一条挡掉 `.`、`..`、
#     `-rf`、`trailing.`（Windows 会吃掉结尾的点）这一整类。
#   * 不含 `/`、`\`、空白、NUL，也不含 `..`。路径穿越在这里就断掉，而不是靠
#     后面那次 realpath 兜底 —— 兜底是第二道，不是第一道。
import re
import unicodedata

# 64 是给人看的上限，不是文件系统的上限：更长的 ID 大概率是拼进来的路径。
MAX_WORLD_ID_LENGTH = 64
WORLD_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class WorldIdError(ValueError):
    """world_id 不能安全地变成存档根之下的一个目录名。"""


def validate_world_id(world_id) -> str:
    """校验并原样返回 world_id。**不做**任何归一化。

    归一化（转小写、NFC、去空白）会把两个不同的输入变成同一个世界，而调用方
    以为它们是两个。这里只回答"能不能用"，不替调用方改主意。
    """
    if not isinstance(world_id, str):
        raise WorldIdError("world_id 必须是字符串")
    if not world_id:
        raise WorldIdError("world_id 不能为空")
    if len(world_id) > MAX_WORLD_ID_LENGTH:
        raise WorldIdError(
            f"world_id 最长 {MAX_WORLD_ID_LENGTH} 个字符，收到 {len(world_id)} 个"
        )
    if not world_id.isascii():
        raise WorldIdError(
            f"world_id 只允许 ASCII：{world_id!r} 有多种等价写法，"
            "在磁盘上会归一成同一个目录"
        )
    if unicodedata.normalize("NFC", world_id) != world_id:
        raise WorldIdError(f"world_id 必须是 NFC 形式: {world_id!r}")
    if not WORLD_ID_PATTERN.match(world_id):
        raise WorldIdError(
            f"world_id 只允许小写字母、数字和 . _ -，且必须以字母或数字开头: "
            f"{world_id!r}"
        )
    if world_id[-1] in "._-":
        raise WorldIdError(f"world_id 不能以 . _ - 结尾: {world_id!r}")
    if ".." in world_id:
        raise WorldIdError(f"world_id 不能包含 '..': {world_id!r}")
    return world_id
