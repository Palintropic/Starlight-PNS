# pns/world/extension.py — 运行中的世界只增不改地扩展地点图（WORLD-2）
#
# 这个模块回答一个问题：**一份扩展定义，加到这张图上，得到的新图是什么，以及它有没有
# 碰到旧世界。** 它是纯函数：不碰会话、不碰 WorldState，提交边界在校验阶段和应用阶段
# 各调一次，两次得到同一张图（同一份输入，同一个结果）。
#
# 扩展定义（事件 payload 里的两个字段）：
#
#   locations           新地点的完整定义（Location.to_dict 的形状），一个都不能跟旧图重名；
#   append_connections  {旧地点 id: [连接…]}——追加在旧地点连接列表末尾，只能连向新地点。
#
# 不变量（设计 v3 §4.2；违反任意一条整份扩展拒绝）：
#
#   1. 旧地点的每个字段原样保留，旧连接一条不改、不删、不重排；
#   2. 任意两个旧地点之间的完整路线（经过的节点、每一跳的分钟数与方式）不变——
#      在最宽松的通行规则下比较，所以经由新地点的任何捷径都会被发现；
#   3. 新地点不公开，新地点不声明在旧地点能听见（audible_from 不指向旧地点）：
#      已有居民可进入、可感知的范围不变。已有居民有没有新地点的授予由提交边界核对，
#      那需要世界里的授予表，不在这个纯函数里。
#   4. 所有旅行时间为正。
from itertools import permutations
from typing import Dict, Iterable, Mapping, Tuple

from pns.models.content_ledger import content_fingerprint
from pns.models.frozen import thaw_json_value
from pns.models.location import Connection, Location, LocationGraph, LocationGraphError
from pns.world.routing import plan_route


class ExtensionError(ValueError):
    """这份扩展不能加到这张图上。"""


def graph_fingerprint(graph: LocationGraph) -> str:
    """地点图的内容指纹：扩展前后各记一次，提交与恢复都拿它核对。"""
    return content_fingerprint(graph.to_dict())


def extended_graph(base: LocationGraph, locations, append_connections) -> LocationGraph:
    """返回 `base` 加上这份扩展之后的新图（未冻结）。不合法就抛 ExtensionError。"""
    if not isinstance(base, LocationGraph):
        raise ExtensionError("扩展的底图必须是 LocationGraph")
    locations = thaw_json_value(locations)
    append_connections = thaw_json_value(append_connections)
    if not isinstance(locations, list) or not locations:
        raise ExtensionError("扩展必须至少新增一个地点")
    if not isinstance(append_connections, dict):
        raise ExtensionError("append_connections 必须是 {旧地点 id: [连接…]}")

    new_locations = []
    for index, entry in enumerate(locations):
        try:
            location = Location.from_dict(entry)
        except (KeyError, TypeError, ValueError) as e:
            raise ExtensionError(f"第 {index + 1} 个新地点定义不合法：{e}") from None
        if location.to_dict() != entry:
            # 缺省值被悄悄补上的定义，存进历史的跟生效的就不是同一份了。
            raise ExtensionError(f"第 {index + 1} 个新地点定义不是完整规范形状")
        if base.has(location.location_id):
            raise ExtensionError(f"新地点 '{location.location_id}' 与已有地点重名")
        new_locations.append(location)
    new_ids = {location.location_id for location in new_locations}
    if len(new_ids) != len(new_locations):
        raise ExtensionError("扩展里有重名的新地点")

    appended: Dict[str, Tuple[Connection, ...]] = {}
    for location_id, entries in append_connections.items():
        if not base.has(location_id):
            raise ExtensionError(f"append_connections 指向了不存在的旧地点 '{location_id}'")
        if not isinstance(entries, list) or not entries:
            raise ExtensionError(f"'{location_id}' 的追加连接必须是非空列表")
        extra = []
        for entry in entries:
            try:
                connection = Connection.from_dict(entry)
            except (KeyError, TypeError, ValueError) as e:
                raise ExtensionError(f"'{location_id}' 的追加连接不合法：{e}") from None
            if connection.to_dict() != entry:
                raise ExtensionError(f"'{location_id}' 的追加连接不是完整规范形状")
            if connection.to_id not in new_ids:
                # 旧→旧的新边就是在旧世界里开一条新路。
                raise ExtensionError(
                    f"'{location_id}' 只能追加连向新地点的连接，收到 '{connection.to_id}'"
                )
            extra.append(connection)
        appended[location_id] = tuple(extra)

    merged = []
    for location in base:
        if location.location_id in appended:
            location = Location.from_dict(
                {
                    **location.to_dict(),
                    "connections": [
                        c.to_dict() for c in location.connections + appended[location.location_id]
                    ],
                }
            )
        merged.append(location)
    try:
        graph = LocationGraph(merged + new_locations)
    except LocationGraphError as e:
        raise ExtensionError(f"扩展后的图不成立：{e}") from None

    _require_positive_minutes(new_locations, appended)
    _require_closed(new_locations, base)
    _require_old_routes_unchanged(base, graph)
    return graph


def _require_positive_minutes(new_locations, appended) -> None:
    connections = [c for location in new_locations for c in location.connections]
    connections += [c for extra in appended.values() for c in extra]
    for connection in connections:
        if connection.travel_minutes <= 0:
            raise ExtensionError(
                f"扩展里通往 '{connection.to_id}' 的连接旅行时间必须为正"
            )


def _require_closed(new_locations: Iterable[Location], base: LocationGraph) -> None:
    for location in new_locations:
        if location.access.get("public") is True:
            raise ExtensionError(
                f"新地点 '{location.location_id}' 是公开的：已有居民的可进入范围会因此改变"
            )
        audible = location.perception.get("audible_from", ())
        if isinstance(audible, str):
            audible = (audible,)
        leaking = sorted(str(item) for item in audible if base.has(str(item)))
        if leaking:
            raise ExtensionError(
                f"新地点 '{location.location_id}' 声明在旧地点 {'、'.join(leaking)} 听得见："
                "已有居民的感知范围会因此改变"
            )


def _require_old_routes_unchanged(base: LocationGraph, graph: LocationGraph) -> None:
    def anywhere(_):
        return True

    for a, b in permutations(sorted(base.ids()), 2):
        if plan_route(graph, anywhere, a, b) != plan_route(base, anywhere, a, b):
            raise ExtensionError(f"扩展改变了旧地点 '{a}' → '{b}' 之间的路线")


__all__ = ["ExtensionError", "extended_graph", "graph_fingerprint"]
