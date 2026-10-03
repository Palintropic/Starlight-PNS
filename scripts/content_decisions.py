# scripts/content_decisions.py — 内容冲突的冷维护（CONTENT-4 过渡设计 v2）
#
# 服务器拒绝恢复一个作息门不齐的世界之后，在同一个容器里用它记下决定：
#
#   docker exec starlight-pns python scripts/content_decisions.py yoake-mae
#   docker exec starlight-pns python scripts/content_decisions.py yoake-mae \
#       --adopt <conflict_id> [<conflict_id> ...]
#
# 不带决定时只读磁盘、列出冲突，不拿所有权、不写任何东西。带决定时独占打开世界
# （不运行、不碰时钟），逐条记录、关闭，再从磁盘读回核对。退出码 0 只表示：这次
# 运行没有任何一步报错，要求的决定都在磁盘上，关闭是 clean、耐久、目录同步有证据
# 的。其他情况一律非 0，照输出处理后重跑；重跑从磁盘起步，已经记下的决定不会再记。
#
# 不要用 -e 覆盖存档根或内容包：脚本必须和服务器看同一个世界。
import argparse
import json
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from process_setup import prepare  # noqa: E402

prepare()

from pns.interfaces.composition import AutonomySettings, WorldControlPlane  # noqa: E402
from pns.interfaces.content_maintenance import (  # noqa: E402
    MaintenanceError,
    read_conflicts,
    run_content_decisions,
)
from pns.interfaces.security import DeploymentSettings  # noqa: E402
from pns.runtime.persistence import WorldAlreadyOwned  # noqa: E402

TERMINAL_NOTE = (
    "注意：驳回和暂缓对这一版内容是终局的。这个世界会一直被挡在门外，"
    "只有内容包里重新出现已采用的那一版、或者出一个新版本，才能解除。"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="对持久世界的内容冲突记下项目所有者的决定")
    parser.add_argument("world_id")
    parser.add_argument("--adopt", nargs="+", default=[], metavar="CONFLICT_ID")
    parser.add_argument("--decline", nargs="+", default=[], metavar="CONFLICT_ID")
    parser.add_argument("--defer", nargs="+", default=[], metavar="CONFLICT_ID")
    parser.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    return parser


def _plane() -> WorldControlPlane:
    # 与 create_app 组装控制面的方式相同：存档根、内容、模型配置都从同一份环境
    # 来。不 import server.py —— 那会顺手建一个 Web 应用。
    deployment = DeploymentSettings.from_env()
    return WorldControlPlane(
        autonomy=AutonomySettings.from_env(production=deployment.production)
    )


def _list(plane: WorldControlPlane, world_id: str, as_json: bool) -> int:
    views = read_conflicts(plane, world_id)
    if as_json:
        print(json.dumps([view.to_dict() for view in views], ensure_ascii=False, indent=2))
        return 0
    if not views:
        print(f"{world_id}: 没有内容冲突记录")
        return 0
    for view in views:
        base = "" if view.current_base else "（针对的已采用版本已过期，不能采用）"
        print(f"{view.conflict_id}  {view.subject}  {view.status}{base}")
    return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    decisions = (
        [(cid, "adopted") for cid in args.adopt]
        + [(cid, "declined") for cid in args.decline]
        + [(cid, "deferred") for cid in args.defer]
    )
    plane = _plane()
    try:
        if not decisions:
            return _list(plane, args.world_id, args.json)
        if args.decline or args.defer:
            print(TERMINAL_NOTE, file=sys.stderr)
        report = run_content_decisions(plane, args.world_id, decisions)
    except WorldAlreadyOwned:
        print(
            f"世界 '{args.world_id}' 正被别的进程持有（多半是服务器）。"
            "先在 dashboard 关闭世界，再运行本脚本。什么都没有改。",
            file=sys.stderr,
        )
        return 2
    except MaintenanceError as e:
        print(f"没有执行：{e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return report.exit_code

    for outcome in report.outcomes:
        on_disk = report.disk_status(outcome.conflict_id)
        line = f"{outcome.conflict_id}  要求 {outcome.requested}  本次 {outcome.result}  磁盘上 {on_disk}"
        if outcome.error:
            line += f"  （{outcome.error}）"
        print(line)
    close = report.close or {}
    print(
        "关闭: "
        + ", ".join(f"{key}={close.get(key)}" for key in ("clean", "durable", "directory_synced"))
    )
    if report.complete:
        print("完成：决定都在磁盘上，关闭 clean、耐久、目录同步有证据。可以回 dashboard 恢复世界。")
    else:
        if report.errors:
            print("本次有操作失败过：")
            for error in report.errors:
                print(f"  - {error}")
        if report.on_disk and not report.strongly_durable:
            print("决定已经在磁盘上，但强耐久没有得到证实。")
        elif report.on_disk:
            print("最终磁盘状态已经补齐。")
        print("未完成：照上面处理后原样重跑。")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
