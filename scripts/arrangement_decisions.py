# scripts/arrangement_decisions.py — 安排采用、修订、撤销的冷维护
import argparse
import json
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from process_setup import prepare

prepare()

from pns.interfaces.arrangement_maintenance import (
    pending_arrangements,
    run_arrangement_decision,
)
from pns.interfaces.composition import AutonomySettings, WorldControlPlane
from pns.interfaces.security import DeploymentSettings


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="持久世界的安排维护；写入前先关闭服务器中的世界"
    )
    parser.add_argument("world_id")
    parser.add_argument("--action", choices=("adopted", "revised", "revoked"))
    parser.add_argument("--character")
    parser.add_argument("--id")
    parser.add_argument("--version", type=int, help="修订或撤销所针对的当前版本")
    args = parser.parse_args(argv)
    if args.action and (not args.character or not args.id):
        parser.error("维护操作必须指定 --character 与 --id")
    deployment = DeploymentSettings.from_env()
    plane = WorldControlPlane(
        autonomy=AutonomySettings.from_env(production=deployment.production)
    )
    try:
        result = (
            run_arrangement_decision(
                plane, args.world_id, args.action, args.character, args.id, args.version
            )
            if args.action
            else pending_arrangements(plane, args.world_id)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not args.action or result["durable"] else 1
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
