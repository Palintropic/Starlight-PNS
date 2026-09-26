# pns/world/routing.py — 位置图上的寻路
#
# 这个模块回答一个问题：**从这里到那里，这个角色能走哪条路、要多久。**
#
# 它不回答：该不该去（Agency / 未来的 Planner）、什么时候出发（作息的行程）、
# 这一步能不能成为世界事实（事件提交边界）。所以它是一个纯函数：不读时钟、
# 不改位置图、不改世界状态，同样的输入永远给出同样的路线。
#
# 三条硬约束：
#
#   1. **权限不在这里重写。** 能不能进入一个地点由调用方传进来的只读谓词回答
#      （通常由 WorldState.may_enter / access_admits 构造）。这里复制一份权限
#      逻辑，迟早会出现"寻路说能走、提交边界说不能进"的两套答案。
#   2. **起点不需要进入资格，其余每个节点都需要。** 人已经在起点了；路上经过的
#      每个地方和终点，都是要以一条位置变更事件真正走进去的。
#   3. **结果是确定的。** 先比总耗时，耗时相同再比整条路径（节点 id 序列）的
#      字典序。平局不能交给字典迭代顺序或堆的实现细节。
import heapq
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from pns.models.location import LocationGraph


class RoutingError(ValueError):
    """寻路请求本身不合法（未知地点等）。无路可走不是错误，返回 None。"""


@dataclass(frozen=True)
class Leg:
    """路线上的一跳：沿一条有向连接从 from_id 走到 to_id。"""

    from_id: str
    to_id: str
    minutes: int
    mode: str


@dataclass(frozen=True)
class Route:
    """一条完整路线。起点等于终点时 legs 为空、耗时为 0。"""

    from_id: str
    to_id: str
    legs: Tuple[Leg, ...]

    @property
    def total_minutes(self) -> int:
        return sum(leg.minutes for leg in self.legs)

    @property
    def nodes(self) -> Tuple[str, ...]:
        return (self.from_id,) + tuple(leg.to_id for leg in self.legs)


def plan_route(
    graph: LocationGraph,
    may_enter: Callable[[str], bool],
    from_id: str,
    to_id: str,
) -> Optional[Route]:
    """找出从 from_id 到 to_id 的最短路线；走不通返回 None。

    `may_enter(location_id)` 回答这个角色能不能进入某个地点。终点和途经的每个
    节点都必须为真；起点不问。
    """
    if not isinstance(graph, LocationGraph):
        raise RoutingError("寻路需要一张 LocationGraph")
    for label, location_id in (("起点", from_id), ("终点", to_id)):
        if not isinstance(location_id, str) or not graph.has(location_id):
            raise RoutingError(f"寻路的{label}不存在: {location_id!r}")
    if from_id == to_id:
        return Route(from_id=from_id, to_id=to_id, legs=())
    if not may_enter(to_id):
        return None

    # 堆里放 (总耗时, 节点序列, 各跳)。按 (耗时, 节点序列) 比较，第一次弹出某个
    # 节点时拿到的就是它在"先耗时、后字典序"意义下的最优路径：同一条连接的
    # 延伸不会改变两条路径的先后。
    # 同一对地点之间可以有多条连接（例如步行与电车）；它们节点序列相同，
    # 先比交通方式，再比入堆顺序（按连接声明顺序，确定），堆永远不需要比较 Leg。
    frontier: List[Tuple[int, Tuple[str, ...], Tuple[str, ...], int, Tuple[Leg, ...]]] = [
        (0, (from_id,), (), 0, ())
    ]
    pushed = 0
    settled = set()
    while frontier:
        minutes, nodes, modes, _order, legs = heapq.heappop(frontier)
        here = nodes[-1]
        if here in settled:
            continue
        settled.add(here)
        if here == to_id:
            return Route(from_id=from_id, to_id=to_id, legs=legs)
        for connection in graph.get(here).connections:
            there = connection.to_id
            if there in settled or not may_enter(there):
                continue
            leg = Leg(
                from_id=here,
                to_id=there,
                minutes=connection.travel_minutes,
                mode=connection.mode,
            )
            pushed += 1
            heapq.heappush(
                frontier,
                (
                    minutes + connection.travel_minutes,
                    nodes + (there,),
                    modes + (connection.mode,),
                    pushed,
                    legs + (leg,),
                ),
            )
    return None


__all__ = ["Leg", "Route", "RoutingError", "plan_route"]
