# pns/interfaces/arrangement_maintenance.py — 安排的独立冷维护入口
from pns.interfaces.content_maintenance import MAINTENANCE_POLICY, MaintenanceError
from pns.models.arrangement import ArrangementDefinition


def pending_arrangements(plane, world_id):
    """只读待采用定义。内容删除不撤销已经采用的快照。"""
    state = plane.service.store.load(world_id).restore_state()
    current = state.arrangements.to_dict()["current"]
    candidates = []
    registry = plane.registry()
    for cid in state.characters:
        for definition in registry.character(cid).arrangements:
            old = current.get(definition.id)
            if old is None or (
                not old["revoked"] and old["fingerprint"] != definition.fingerprint
            ):
                candidates.append(
                    {
                        "character_id": cid,
                        "action": "adopted" if old is None else "revised",
                        "expected_version": old["version"] if old else None,
                        "definition": definition.to_dict(),
                    }
                )
    return candidates


def run_arrangement_decision(
    plane, world_id, action, character_id, arrangement_id, expected_version=None
):
    """独占打开、维护并 checkpoint、关闭后从盘上重读。任何报错都不报耐久成功。"""
    if action not in ("adopted", "revised", "revoked"):
        raise MaintenanceError("未知安排操作")
    registry = plane.registry()
    definition = None
    if action != "revoked":
        matches = [
            d
            for d in registry.character(character_id).arrangements
            if d.id == arrangement_id
        ]
        if len(matches) != 1:
            raise MaintenanceError("当前内容没有这条安排定义")
        definition = matches[0].to_dict()
    world = plane.service.restore(
        world_id,
        adapters=plane.build_adapters(registry, world_id=world_id),
        checkpoint_policy=MAINTENANCE_POLICY,
        clock=None,
        start=False,
    )
    report = {
        "world_id": world_id,
        "action": action,
        "arrangement_id": arrangement_id,
        "errors": [],
        "checkpoint": None,
        "close": None,
        "durable": False,
    }
    try:
        report["checkpoint"] = world.maintain_arrangement(
            action,
            character_id,
            definition=definition,
            arrangement_id=arrangement_id,
            expected_version=expected_version,
        )
    except Exception as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        try:
            report["close"] = world.close("arrangement_maintenance")
        except Exception as error:
            report["errors"].append(f"{type(error).__name__}: {error}")
    if not report["errors"]:
        try:
            disk = plane.service.store.load(world_id).restore_state()
            entry = disk.arrangements.to_dict()["current"][arrangement_id]
            if entry["character_id"] != character_id or entry["action"] != action:
                raise MaintenanceError("磁盘上的安排操作不匹配")
            if (
                definition is not None
                and entry["fingerprint"]
                != ArrangementDefinition(definition).fingerprint
            ):
                raise MaintenanceError("磁盘上的安排定义不匹配")
            report["durable"] = bool(
                report["checkpoint"]["durable"]
                and report["close"]["durable"]
                and report["close"]["clean"]
            )
        except Exception as error:
            report["errors"].append(f"{type(error).__name__}: {error}")
    return report
