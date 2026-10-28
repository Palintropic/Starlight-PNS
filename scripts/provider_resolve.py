#!/usr/bin/env python3
"""CFG-1：模型设置保存没收尾时的服务器侧恢复（服务没在跑的时候用）。

生产启动发现 data/settings/provider.txn 会拒绝起来。这时在**服务停着**的情况下，
用同一个镜像、同一个数据卷跑这个脚本（compose 里只有一个写者，别在服务跑着时用）：

    # 看现场：provider.json / .prev 是否存在、摘要是否与事务记录一致
    provider_resolve.py show

    # 选定保留哪一份，构建校验通过后清掉 .txn / .prev
    provider_resolve.py resolve --keep previous   # 用 .prev 恢复事务开始前的内容
    provider_resolve.py resolve --keep current    # 留下现在的 provider.json
    provider_resolve.py resolve --keep absent     # 恢复为不存在（回到 .env 派生）

跟在线的"解除隔离"走同一段代码：写入所选内容 → 完整构建并校验 → 耐久清理。
任何一步失败都保留 .txn，服务照样起不来，不会半途放行。直接删掉 provider.txn
不算恢复完成。

退出码：0 = 已恢复；2 = 所选内容无法核实或没有需要恢复的事务；3 = 写入、构建或
清理失败，事务仍在；1 = 其它错误。
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pns.runtime import content_registry, provider_txn  # noqa: E402
from pns.runtime.reload import ConfigBoundary, SessionSupervisor  # noqa: E402


def _show(settings_dir: Path) -> int:
    files = {
        "provider.json": settings_dir / "provider.json",
        provider_txn.PREV_FILE: settings_dir / provider_txn.PREV_FILE,
    }
    txn = settings_dir / provider_txn.TXN_FILE
    try:
        record = json.loads(txn.read_bytes()) if txn.exists() else None
    except (OSError, ValueError):
        record = "unreadable"
    report = {
        "txn": record if isinstance(record, dict) or record is None else "unreadable",
        "files": {
            name: provider_txn.digest(path.read_bytes()) if path.exists() else provider_txn.ABSENT
            for name, path in files.items()
        },
    }
    # 只有摘要和事务记录里的元数据：文件内容带 token，不打印。
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--keep", choices=provider_txn.CHOICES, required=True)
    args = parser.parse_args(argv)

    settings_dir = content_registry.SETTINGS_DIR
    if args.command == "show":
        return _show(settings_dir)
    boundary = ConfigBoundary(SessionSupervisor())
    try:
        outcome = provider_txn.resolve(boundary, settings_dir, args.keep, reason="服务器侧恢复")
    except (provider_txn.NothingToResolve, provider_txn.ResolveRefused) as e:
        print(f"未改动：{e}", file=sys.stderr)
        return 2
    if outcome.status == provider_txn.OUTCOME_OK:
        print("已恢复：provider.txn 已清除，可以启动服务。")
        return 0
    detail = outcome.reload.error if outcome.reload is not None and outcome.reload.error else ""
    print(f"恢复没有完成，provider.txn 仍在。{detail}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
