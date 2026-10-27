# pns/runtime/version_switch.py — 最近一次"旧版本 → 新版本"切换的时刻（CFG-1 §9）
#
# 回滚到旧镜像期间，旧代码按另一套口径估余额（一个锚点扣全部行），操作员也可能
# 在旧面板上重填过余额。切回新版本之后，早于这次切换的按账户锚点该提醒重填。
#
# 镜像里没有版本号，但账本能说明谁写过：新代码写的每一行都带 `account`，旧代码
# 写的没有。所以生产启动时：
#
#   * 还没有记录（第一次升级上来，或记录读不出来）—— 这次启动就是一次切换；
#   * 账本最后一行有效记录没有 `account`，并且晚于上次记下的切换 —— 上次之后
#     跑过旧代码，这次启动又是一次切换。
#
# 旧代码在回滚期间一次模型调用都没发过，就看不出来；那段时间也没有花费被按旧
# 口径扣，漏掉无妨。
#
# 只是提醒：写不下来不拦启动（调用方吞掉写盘错误）。
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from pns.runtime import settings_files
from pns.runtime.usage_ledger import UsageLedger, _parse_at

SWITCH_FILE = "version_switch.json"
SWITCH_VERSION = 1


def read_switch(usage_root: Path) -> Optional[datetime]:
    """记下的切换时刻；没有或读不出来都是 None。"""
    try:
        raw = json.loads((Path(usage_root) / SWITCH_FILE).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != SWITCH_VERSION:
            return None
        return _parse_at(raw.get("switched_at"))
    except (OSError, ValueError):
        return None


def note_startup(ledger: UsageLedger, now: datetime) -> datetime:
    """生产启动时调一次。返回此后生效的切换时刻（这次记下的，或之前的）。

    写盘按三档合同抛出；调用方决定要不要吞。
    """
    previous = read_switch(ledger.root)
    if previous is not None:
        last = ledger.latest()
        if last is None or last.account is not None or last.at <= previous:
            return previous
    payload = {"version": SWITCH_VERSION, "switched_at": now.isoformat()}
    settings_files.durable_write(
        Path(ledger.root) / SWITCH_FILE,
        (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
    )
    return now


__all__ = ["SWITCH_FILE", "note_startup", "read_switch"]
