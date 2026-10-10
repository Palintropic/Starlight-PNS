# pns/models/arrangement.py — 安排定义、采用审计与 occurrence 账
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Mapping

from pns.models.activation import ScheduledActivation, ActivationKind
from pns.models.frozen import freeze_json_value, thaw_json_value

ARRANGEMENT_PREFIX = "arrangement.activation:"
RELATIVE_WORDS = (
    "今天",
    "明天",
    "后天",
    "昨天",
    "今晚",
    "明晚",
    "今夜",
    "今日",
    "明日",
    "今晩",
    "昨日",
    "昨晚",
    "前天",
    "明後日",
    "明後天",
)
LAPSE_REASONS = {"arrangement_expired", "arrangement_revoked", "arrangement_superseded"}


class ArrangementError(ValueError):
    """安排定义或持久化证据不合法。"""


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def sim(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as e:
        raise ArrangementError("模拟时间不合法") from e
    if (
        not isinstance(result, datetime)
        or result.tzinfo is not None
        or result.second
        or result.microsecond
    ):
        raise ArrangementError("安排时间必须是无时区整分钟")
    return result


def identity(arrangement_id, version, start):
    if (
        not isinstance(arrangement_id, str)
        or not arrangement_id
        or type(version) is not int
        or version <= 0
    ):
        raise ArrangementError("occurrence 身份不合法")
    return {
        "arrangement_id": arrangement_id,
        "version": version,
        "start": sim(start).isoformat(),
    }


def activation_id(occ):
    if not isinstance(occ, Mapping) or set(occ) != {
        "arrangement_id",
        "version",
        "start",
    }:
        raise ArrangementError("occurrence 身份字段不合法")
    identity(occ["arrangement_id"], occ["version"], occ["start"])
    return ARRANGEMENT_PREFIX + json.dumps(
        thaw_json_value(occ), sort_keys=True, separators=(",", ":")
    )


@dataclass(frozen=True)
class ArrangementDefinition:
    data: Mapping

    def __post_init__(self):
        raw = thaw_json_value(self.data)
        allowed = {
            "id",
            "cue",
            "source",
            "evidence",
            "weekly",
            "once",
            "window_minutes",
        }
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ArrangementError("安排包含未知字段")
        for key in ("id", "cue", "source"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ArrangementError(f"安排缺少 {key}")
        if raw["source"] != "inferred":
            raise ArrangementError("安排 source 必须是 inferred")
        if len(raw["cue"]) > 200 or any(word in raw["cue"] for word in RELATIVE_WORDS):
            raise ArrangementError("cue 太长或含相对日期词")
        if ("weekly" in raw) == ("once" in raw):
            raise ArrangementError("必须且只能声明 weekly / once 之一")
        window = raw.setdefault("window_minutes", 120)
        if type(window) is not int or window <= 0:
            raise ArrangementError("window_minutes 必须是正整数")
        key = "weekly" if "weekly" in raw else "once"
        rule = raw[key]
        if not isinstance(rule, dict) or set(rule) != (
            {"weekday", "at"} if key == "weekly" else {"date", "at"}
        ):
            raise ArrangementError("recurrence 形状不合法")
        at = rule["at"]
        if not isinstance(at, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", at
        ):
            raise ArrangementError("at 必须是 HH:MM")
        if key == "weekly":
            if type(rule["weekday"]) is not int or not 0 <= rule["weekday"] <= 6:
                raise ArrangementError("weekday 必须是 0–6")
        else:
            day = rule["date"]
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ArrangementError("date 必须是 YYYY-MM-DD")
            sim(day + "T" + at)
        object.__setattr__(self, "data", freeze_json_value(raw, error=ArrangementError))

    def to_dict(self):
        return thaw_json_value(self.data)

    @property
    def id(self):
        return self.data["id"]

    @property
    def fingerprint(self):
        return fingerprint(self.to_dict())

    def next_after(self, after):
        after = sim(after)
        rule = self.data.get("weekly") or self.data["once"]
        hour, minute = map(int, rule["at"].split(":"))
        if "weekly" in self.data:
            candidate = after.replace(hour=hour, minute=minute) + timedelta(
                days=(rule["weekday"] - after.weekday()) % 7
            )
            if candidate <= after:
                candidate += timedelta(days=7)
            return candidate
        candidate = sim(rule["date"] + "T" + rule["at"])
        return candidate if candidate > after else None

    def projection(self, start):
        start = sim(start)
        return {
            "cue": f"【安排·{start:%Y年%m月%d日 %H:%M}】" + self.data["cue"],
            "date_binding": start.date().isoformat(),
            "source": self.data["source"],
        }


def parse_arrangements(raw):
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ArrangementError("arrangements 必须是数组")
    definitions = tuple(ArrangementDefinition(item) for item in raw)
    if len({item.id for item in definitions}) != len(definitions):
        raise ArrangementError("重复 arrangement id")
    return definitions


class ArrangementBook:
    """会话拥有的审计账。通过事务内替换更新，旧值不会被就地改写。"""

    def __init__(self, payload=None):
        raw = (
            payload
            if payload is not None
            else {"operations": [], "current": {}, "occurrences": {}}
        )
        if not isinstance(raw, Mapping) or set(raw) != {
            "operations",
            "current",
            "occurrences",
        }:
            raise ArrangementError("安排簿形状不合法")
        if (
            not isinstance(raw["operations"], (list, tuple))
            or not isinstance(raw["current"], Mapping)
            or not isinstance(raw["occurrences"], Mapping)
        ):
            raise ArrangementError("安排簿字段形状不合法")
        self._data = freeze_json_value(raw, error=ArrangementError)

    def to_dict(self):
        return thaw_json_value(self._data)

    @property
    def sequence(self):
        return len(self._data["operations"])

    @property
    def statistics(self):
        values = self._data["occurrences"].values()
        return {
            "expired_occurrences": sum(o["status"] == "expired" for o in values),
            "missed_occurrences": sum(
                o["status"] == "expired" and not o["offered"] for o in values
            ),
            "revoked_occurrences": sum(o["status"] == "revoked" for o in values),
        }

    def scheduled(self, entry):
        at = entry["next_due_at"]
        if entry["revoked"] or at is None:
            return None
        occ = identity(entry["definition"]["id"], entry["version"], at)
        definition = ArrangementDefinition(entry["definition"])
        return ScheduledActivation(
            activation_id(occ),
            ActivationKind.CHARACTER_ACTIVATION,
            sim(at),
            entry["character_id"],
            payload={**occ, **definition.projection(at)},
        )

    def operate(
        self,
        action,
        character_id,
        at,
        definition=None,
        arrangement_id=None,
        expected_version=None,
    ):
        raw = self.to_dict()
        definition = (
            ArrangementDefinition(definition) if definition is not None else None
        )
        aid = definition.id if definition is not None else arrangement_id
        old = raw["current"].get(aid)
        if action == "adopted":
            if old is not None:
                raise ArrangementError("安排身份已被使用")
            version = 1
        elif action in ("revised", "revoked"):
            if (
                old is None
                or old["revoked"]
                or type(expected_version) is not int
                or old["version"] != expected_version
            ):
                raise ArrangementError("安排当前版本不匹配")
            if character_id != old["character_id"]:
                raise ArrangementError("安排不能换目标")
            version = old["version"] + (action == "revised")
            if action == "revised" and (
                definition is None or definition.fingerprint == old["fingerprint"]
            ):
                raise ArrangementError("修订必须改变定义")
        else:
            raise ArrangementError("未知维护操作")
        if action == "revoked":
            definition = ArrangementDefinition(old["definition"])
            next_at = None
        else:
            next_at = definition.next_after(at)
            if next_at is None:
                raise ArrangementError("once 不晚于生效时刻")
        operation = {
            "sequence": len(raw["operations"]) + 1,
            "action": action,
            "at": sim(at).isoformat(),
            "character_id": character_id,
            "previous_version": old["version"] if old else None,
            "version": version,
            "fingerprint": definition.fingerprint,
            "definition": definition.to_dict(),
        }
        raw["operations"].append(operation)
        raw["current"][aid] = {
            **operation,
            "revoked": action == "revoked",
            "next_due_at": next_at.isoformat() if next_at else None,
        }
        for occurrence in raw["occurrences"].values():
            if (
                occurrence["identity"]["arrangement_id"] == aid
                and occurrence["status"] == "open"
            ):
                occurrence.update(
                    status="revoked",
                    terminal={
                        "reason": "superseded" if action == "revised" else "revoked",
                        "at": sim(at).isoformat(),
                        "evaluated_until": sim(at).isoformat(),
                        "operation_sequence": operation["sequence"],
                    },
                )
        return ArrangementBook(raw)

    def fire(self, activation, due):
        raw = self.to_dict()
        aid = activation.payload["arrangement_id"]
        entry = raw["current"][aid]
        if self.scheduled(entry) != activation:
            raise ArrangementError("安排触发不是当前下一次")
        definition = ArrangementDefinition(entry["definition"])
        following = definition.next_after(activation.due_at)
        entry["next_due_at"] = following.isoformat() if following else None
        occ = identity(aid, entry["version"], activation.due_at)
        key = activation_id(occ)
        if key in raw["occurrences"]:
            raise ArrangementError("occurrence 不能重开")
        raw["occurrences"][key] = {
            "identity": occ,
            "character_id": entry["character_id"],
            "fingerprint": entry["fingerprint"],
            **definition.projection(activation.due_at),
            "expires_at": (
                activation.due_at + timedelta(minutes=definition.data["window_minutes"])
            ).isoformat(),
            "due_id": due.due_id,
            "operation_sequence": entry["sequence"],
            "status": "open",
            "terminal": None,
            "offered": {},
            "cognition_offered": 0,
        }
        return ArrangementBook(raw)

    def expire(self, at, eligibility_at):
        raw = self.to_dict()
        for occurrence in raw["occurrences"].values():
            if occurrence["status"] == "open" and sim(eligibility_at) > sim(
                occurrence["expires_at"]
            ):
                occurrence.update(
                    status="expired",
                    terminal={
                        "reason": "expired",
                        "at": sim(at).isoformat(),
                        "evaluated_until": sim(eligibility_at).isoformat(),
                        "operation_sequence": self.sequence,
                    },
                )
        return ArrangementBook(raw)

    def carried(self, character_id, at, eligibility_at):
        result = []
        for occurrence in self._data["occurrences"].values():
            if (
                occurrence["character_id"] == character_id
                and occurrence["status"] == "open"
                and sim(occurrence["identity"]["start"])
                <= eligibility_at
                <= sim(occurrence["expires_at"])
            ):
                result.append(
                    {
                        "identity": thaw_json_value(occurrence["identity"]),
                        "offered_at": sim(at).isoformat(),
                        "eligibility_at": sim(eligibility_at).isoformat(),
                        "operation_sequence": self.sequence,
                        "fingerprint": occurrence["fingerprint"],
                        "cue_fingerprint": fingerprint(occurrence["cue"]),
                        "date_binding": occurrence["date_binding"],
                    }
                )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item["identity"]["start"],
                    item["identity"]["arrangement_id"],
                    item["identity"]["version"],
                ),
            )
        )

    def cues(self, carried):
        return freeze_json_value(
            [
                {
                    "cue": self._data["occurrences"][activation_id(item["identity"])][
                        "cue"
                    ],
                    "source": "inferred",
                }
                for item in carried
            ],
            error=ArrangementError,
        )

    def offer(self, record):
        raw = self.to_dict()
        for evidence in record.arrangements_carried:
            occurrence = raw["occurrences"][activation_id(evidence["identity"])]
            if record.due_id in occurrence["offered"]:
                raise ArrangementError("重复提供证据")
            occurrence["offered"][record.due_id] = thaw_json_value(evidence)
            occurrence["cognition_offered"] = len(occurrence["offered"])
        return ArrangementBook(raw)

    def lapse(self, due, at, eligibility_at):
        if not due.activation_id.startswith(ARRANGEMENT_PREFIX):
            return None
        occurrence = self._data["occurrences"].get(due.activation_id)
        if occurrence is None:
            raise ArrangementError("安排 due 没有 occurrence")
        terminal = occurrence["terminal"]
        if occurrence["status"] == "open":
            if eligibility_at <= sim(occurrence["expires_at"]):
                return None
            terminal = {
                "reason": "expired",
                "at": sim(at).isoformat(),
                "evaluated_until": sim(eligibility_at).isoformat(),
                "operation_sequence": self.sequence,
            }
        return {
            "reason": "arrangement_" + terminal["reason"],
            "occurrence": thaw_json_value(occurrence["identity"]),
            "expires_at": occurrence["expires_at"],
            "terminal": thaw_json_value(terminal),
        }

    def valid_carried(self, carried, character_id, at, eligibility_at):
        current = self.carried(character_id, at, eligibility_at)
        valid = {
            (
                activation_id(item["identity"]),
                item["fingerprint"],
                item["cue_fingerprint"],
                item["date_binding"],
            )
            for item in current
        }
        return all(
            (
                activation_id(item["identity"]),
                item["fingerprint"],
                item["cue_fingerprint"],
                item["date_binding"],
            )
            in valid
            for item in carried
        )


def legitimate_lapsed(record):
    """免费形状只认完整的安排失效证据；与会话的对应关系由恢复和引擎复核。"""
    try:
        detail = record.detail
        terminal = detail["terminal"]
        occ = detail["occurrence"]
        if (
            set(occ) != {"arrangement_id", "version", "start"}
            or not isinstance(occ["arrangement_id"], str)
            or not occ["arrangement_id"]
            or type(occ["version"]) is not int
            or occ["version"] <= 0
            or set(terminal)
            != {"reason", "at", "evaluated_until", "operation_sequence"}
        ):
            return False
        fired_at = sim(record.due_id[len(activation_id(occ)) + 1 :])
        return (
            getattr(record.outcome, "value", None) == "lapsed"
            and not record.policy
            and record.proposal is None
            and record.event_id is None
            and not record.arrangements_carried
            and set(detail).issubset(
                {"reason", "occurrence", "expires_at", "terminal", "cognition_interval"}
            )
            and detail["reason"] in LAPSE_REASONS
            and detail["reason"] == "arrangement_" + terminal["reason"]
            and record.due_id.startswith(activation_id(occ) + "@")
            and sim(occ["start"]) <= fired_at <= record.decided_at
            and fired_at <= sim(terminal["at"]) <= record.decided_at
            and sim(occ["start"]) <= sim(detail["expires_at"])
            and type(terminal["operation_sequence"]) is int
            and terminal["operation_sequence"] >= 1
            and (
                terminal["reason"] != "expired"
                or sim(terminal["evaluated_until"]) > sim(detail["expires_at"])
            )
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def verify_arrangements(state):
    """从操作和时间历史重建应有集合，再双向核对持久化投影与提供证据。"""
    from pns.models.event import EventType

    raw = state.arrangements.to_dict()
    operations = raw["operations"]
    clock = state.world_state.clock if state.world_state else None
    target = state.catch_up_target
    if target is not None and (clock is None or sim(target) < clock):
        raise ArrangementError("补跑目标早于世界时钟")
    replay = ArrangementBook()
    previous_at = None
    expected = {}
    current_ops = {}
    for index, op in enumerate(operations, 1):
        if set(op) != {
            "sequence",
            "action",
            "at",
            "character_id",
            "previous_version",
            "version",
            "fingerprint",
            "definition",
        }:
            raise ArrangementError("操作字段不完整")
        if (
            type(op["sequence"]) is not int
            or type(op["version"]) is not int
            or (
                op["previous_version"] is not None
                and type(op["previous_version"]) is not int
            )
        ):
            raise ArrangementError("操作序号与版本必须是整数")
        at = sim(op["at"])
        if (
            clock is None
            or at > clock
            or (previous_at is not None and at < previous_at)
        ):
            raise ArrangementError("操作生效边界不合法")
        if op["character_id"] not in state.characters:
            raise ArrangementError("安排目标不是住民")
        definition = ArrangementDefinition(op["definition"])
        replay = replay.operate(
            op["action"],
            op["character_id"],
            at,
            definition.to_dict() if op["action"] != "revoked" else None,
            definition.id,
            op["previous_version"],
        )
        if replay.to_dict()["operations"][-1] != op:
            raise ArrangementError("操作日志不能重放")
        previous_at = at
        current_ops[definition.id] = op
        if op["action"] == "revoked":
            continue
        end = clock
        for later in operations[index:]:
            if later["definition"]["id"] == definition.id:
                end = sim(later["at"])
                break
        start = definition.next_after(at)
        while start is not None and start <= end:
            occ = identity(definition.id, op["version"], start)
            expected[activation_id(occ)] = (op, definition, start)
            start = definition.next_after(start)
    if set(raw["occurrences"]) != set(expected):
        raise ArrangementError("应有 occurrence 集合不一致")
    entries = replay.to_dict()["current"]
    time_events = [
        event
        for event in state.events.events()
        if event.type is EventType.WORLD_TIME_ADVANCED
    ]
    arrangement_due = {
        d.activation_id: d
        for d in state.activation_outbox.records()
        if d.activation_id.startswith(ARRANGEMENT_PREFIX)
    }
    if len(arrangement_due) != sum(
        d.activation_id.startswith(ARRANGEMENT_PREFIX)
        for d in state.activation_outbox.records()
    ) or set(arrangement_due) != set(expected):
        raise ArrangementError("occurrence / due 集合不一致")
    triggered = {}
    for event in time_events:
        for aid in event.provenance.get("due_activations", ()):
            if aid.startswith(ARRANGEMENT_PREFIX):
                if aid in triggered:
                    raise ArrangementError("重复时间触发")
                triggered[aid] = event
    if set(triggered) != set(expected):
        raise ArrangementError("occurrence / 时间触发集合不一致")
    for key, (op, definition, start) in expected.items():
        occurrence = raw["occurrences"][key]
        if activation_id(occurrence["identity"]) != key:
            raise ArrangementError("occurrence 身份不匹配")
        due = arrangement_due[key]
        identity(
            due.payload["arrangement_id"], due.payload["version"], due.payload["start"]
        )
        event = triggered[key]
        following = definition.next_after(start)
        projection = definition.projection(start)
        canonical = {
            "identity": identity(definition.id, op["version"], start),
            "character_id": op["character_id"],
            "fingerprint": op["fingerprint"],
            **projection,
            "expires_at": (
                start + timedelta(minutes=definition.data["window_minutes"])
            ).isoformat(),
            "due_id": due.due_id,
            "operation_sequence": op["sequence"],
        }
        if set(occurrence) != set(canonical) | {
            "status",
            "terminal",
            "offered",
            "cognition_offered",
        } or any(occurrence.get(k) != v for k, v in canonical.items()):
            raise ArrangementError("occurrence 投影不一致")
        if (
            due.character_id != op["character_id"]
            or due.due_at != start
            or due.missed_occurrences != 0
            or due.next_due_at != following
            or thaw_json_value(due.payload) != {**canonical["identity"], **projection}
            or due.fired_at.isoformat() != event.provenance.get("advanced_to")
            or event.occurred_at >= start
            or due.fired_at < start
        ):
            raise ArrangementError("安排 due / 触发证据不一致")
        terminal = occurrence["terminal"]
        status = occurrence["status"]
        later_ops = [
            o
            for o in operations[op["sequence"] :]
            if o["definition"]["id"] == definition.id
        ]
        if status == "open":
            if (
                terminal is not None
                or later_ops
                or max(clock, target or clock) > sim(occurrence["expires_at"])
            ):
                raise ArrangementError("open occurrence 已失效")
        elif status in ("expired", "revoked"):
            if not isinstance(terminal, dict) or set(terminal) != {
                "reason",
                "at",
                "evaluated_until",
                "operation_sequence",
            }:
                raise ArrangementError("终态证据缺失")
            evaluated = sim(terminal["evaluated_until"])
            terminal_at = sim(terminal["at"])
            seq = terminal["operation_sequence"]
            if (
                type(seq) is not int
                or not op["sequence"] <= seq <= len(operations)
                or not due.fired_at <= terminal_at <= clock
                or evaluated < terminal_at
            ):
                raise ArrangementError("终态边界不合法")
            if evaluated > max(clock, target or clock):
                raise ArrangementError("未完成补跑目标丢失")
            if status == "expired":
                if terminal["reason"] != "expired" or evaluated <= sim(
                    occurrence["expires_at"]
                ):
                    raise ArrangementError("过期依据不成立")
                if later_ops and terminal_at > sim(later_ops[0]["at"]):
                    raise ArrangementError("撤销后的 occurrence 不能再过期")
            else:
                if (
                    not later_ops
                    or later_ops[0]["sequence"] != seq
                    or later_ops[0]["at"] != terminal["at"]
                    or terminal["reason"]
                    != (
                        "superseded"
                        if later_ops[0]["action"] == "revised"
                        else "revoked"
                    )
                ):
                    raise ArrangementError("撤销依据不成立")
        else:
            raise ArrangementError("未知 occurrence 状态")
    # 当前簿的下一次从最后一个操作重新推算，不能只信留下来的队列。
    for aid, entry in entries.items():
        if not entry["revoked"]:
            definition = ArrangementDefinition(entry["definition"])
            start = definition.next_after(sim(entry["at"]))
            while start is not None and start <= clock:
                start = definition.next_after(start)
            entry["next_due_at"] = start.isoformat() if start else None
    for entry in raw["current"].values():
        if (
            type(entry["version"]) is not int
            or type(entry["sequence"]) is not int
            or type(entry["revoked"]) is not bool
        ):
            raise ArrangementError("当前簿字段类型不合法")
    if entries != raw["current"]:
        raise ArrangementError("日志 / 当前簿不一致")
    wanted = {
        activation.activation_id: activation
        for entry in entries.values()
        for activation in [state.arrangements.scheduled(entry)]
        if activation is not None
    }
    actual = {
        a.activation_id: a
        for a in state.activations.pending()
        if a.activation_id.startswith(ARRANGEMENT_PREFIX)
    }
    for activation in actual.values():
        identity(
            activation.payload["arrangement_id"],
            activation.payload["version"],
            activation.payload["start"],
        )
    if wanted != actual:
        raise ArrangementError("安排未来队列不一致")
    offered = {key: {} for key in expected}
    for record in state.agency.records():
        carried = thaw_json_value(record.arrangements_carried)
        if record.outcome.value == "lapsed":
            if not legitimate_lapsed(record):
                raise ArrangementError("LAPSED 形状不合法")
            due = state.activation_outbox.get(record.due_id)
            occurrence = raw["occurrences"].get(due.activation_id)
            if (
                occurrence is None
                or occurrence["terminal"] != thaw_json_value(record.detail["terminal"])
                or record.detail["expires_at"] != occurrence["expires_at"]
                or thaw_json_value(record.detail["occurrence"])
                != occurrence["identity"]
            ):
                raise ArrangementError("LAPSED 失效证据不一致")
        seen = set()
        for evidence in carried:
            if not isinstance(evidence, dict) or set(evidence) != {
                "identity",
                "offered_at",
                "eligibility_at",
                "operation_sequence",
                "fingerprint",
                "cue_fingerprint",
                "date_binding",
            }:
                raise ArrangementError("携带证据缺字段")
            key = activation_id(evidence["identity"])
            if key in seen or key not in expected:
                raise ArrangementError("携带身份不合法")
            seen.add(key)
            occurrence = raw["occurrences"][key]
            seq = evidence["operation_sequence"]
            at, eligibility = sim(evidence["offered_at"]), sim(
                evidence["eligibility_at"]
            )
            if type(seq) is not int or not occurrence[
                "operation_sequence"
            ] <= seq <= len(operations):
                raise ArrangementError("提供操作序号不合法")
            if any(sim(o["at"]) < at for o in operations[seq:]):
                raise ArrangementError("提供时漏掉已经生效的操作")
            if (
                occurrence["terminal"] is not None
                and sim(occurrence["terminal"]["at"]) < at
            ):
                raise ArrangementError("终态之后不能提供")
            if any(
                o["definition"]["id"] == evidence["identity"]["arrangement_id"]
                for o in operations[occurrence["operation_sequence"] : seq]
            ):
                raise ArrangementError("提供时版本已经失效")
            if (
                occurrence["character_id"] != record.character_id
                or at > record.decided_at
                or eligibility < at
                or not sim(occurrence["identity"]["start"])
                <= at
                <= eligibility
                <= sim(occurrence["expires_at"])
                or evidence["fingerprint"] != occurrence["fingerprint"]
                or evidence["cue_fingerprint"] != fingerprint(occurrence["cue"])
                or evidence["date_binding"] != occurrence["date_binding"]
                or sim(operations[seq - 1]["at"]) > at
            ):
                raise ArrangementError("提供窗口或投影不一致")
            offered[key][record.due_id] = evidence
    for key, occurrence in raw["occurrences"].items():
        if (
            occurrence["offered"] != offered[key]
            or type(occurrence["cognition_offered"]) is not int
            or occurrence["cognition_offered"] != len(offered[key])
        ):
            raise ArrangementError("offered / Agency 证据不一致")
