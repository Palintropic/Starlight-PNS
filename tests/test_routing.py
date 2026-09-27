# tests/test_routing.py — 位置图寻路（WORLD-1 行程与未来 Planner 共用）。
#
# 守的线：
#   1. 只走得进去的地方：终点和途经节点都要过 may_enter，起点不问；
#   2. 先比耗时、再比整条路径的字典序，结果确定；
#   3. 纯函数：不改位置图，不读世界之外的任何东西。
#
# 运行: python -m unittest discover -s tests -p test_routing.py
import unittest
from datetime import datetime

from pns.models.location import Connection, Location, LocationGraph
from pns.models.world_state import WorldState
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph
from pns.world.routing import RoutingError, plan_route


def _open(_location_id):
    return True


def _diamond(*, left=5, right=5, extra=()):
    """a → {b, c} → d，两条路可以调成等长。"""
    return LocationGraph(
        (
            Location(
                "a",
                "A",
                connections=(
                    Connection("c", travel_minutes=right),
                    Connection("b", travel_minutes=left),
                )
                + tuple(extra),
            ),
            Location("b", "B", connections=(Connection("d", travel_minutes=1),)),
            Location("c", "C", connections=(Connection("d", travel_minutes=1),)),
            Location("d", "D"),
        )
    )


class ShortestAndDeterministicTests(unittest.TestCase):
    def test_the_default_graph_routes_mizuki_to_school(self):
        route = plan_route(
            build_default_location_graph(), _open, "mizuki_home_room", "kamiyama_high"
        )
        self.assertEqual(
            route.nodes,
            (
                "mizuki_home_room",
                "mizuki_home",
                "city_streets",
                "kamiyama_high_gate",
                "kamiyama_high",
            ),
        )
        self.assertEqual(route.total_minutes, 16)
        self.assertEqual([leg.minutes for leg in route.legs], [1, 12, 1, 2])

    def test_the_shorter_path_wins(self):
        route = plan_route(_diamond(left=9, right=2), _open, "a", "d")
        self.assertEqual(route.nodes, ("a", "c", "d"))

    def test_an_equal_cost_tie_goes_to_the_smaller_node_sequence(self):
        # c 的连接先声明，但 b 在字典序上更小：平局不能取决于声明顺序。
        route = plan_route(_diamond(left=5, right=5), _open, "a", "d")
        self.assertEqual(route.nodes, ("a", "b", "d"))

    def test_the_same_request_always_gives_the_same_route(self):
        graph = build_default_location_graph()
        routes = {
            plan_route(graph, _open, "ena_home_studio", "clothing_store_floor")
            for _ in range(20)
        }
        self.assertEqual(len(routes), 1)

    def test_parallel_connections_do_not_break_the_ordering(self):
        graph = _diamond(extra=(Connection("b", travel_minutes=5, mode="bus"),))
        route = plan_route(graph, _open, "a", "d")
        self.assertEqual(route.nodes, ("a", "b", "d"))
        self.assertEqual(route.legs[0].mode, "bus")  # "bus" < "walk"

    def test_zero_length_route(self):
        route = plan_route(_diamond(), _open, "b", "b")
        self.assertEqual(route.legs, ())
        self.assertEqual(route.total_minutes, 0)


class EntryRulesTests(unittest.TestCase):
    def test_a_blocked_waypoint_forces_the_other_path(self):
        route = plan_route(_diamond(left=1, right=9), lambda l: l != "b", "a", "d")
        self.assertEqual(route.nodes, ("a", "c", "d"))

    def test_no_enterable_path_is_none_not_an_error(self):
        self.assertIsNone(
            plan_route(_diamond(), lambda l: l not in ("b", "c"), "a", "d")
        )

    def test_the_destination_must_be_enterable(self):
        self.assertIsNone(plan_route(_diamond(), lambda l: l != "d", "a", "d"))

    def test_the_origin_is_never_asked(self):
        asked = []

        def may_enter(location_id):
            asked.append(location_id)
            return True

        plan_route(_diamond(), may_enter, "a", "d")
        self.assertNotIn("a", asked)

    def test_real_grants_shape_the_route(self):
        # 用真正的 WorldState.may_enter：没有学生授予就到不了教室，给了就能到。
        world = WorldState(
            clock=datetime(2026, 9, 26, 11, 0),
            locations=build_default_location_graph(),
            channels=build_default_channel_registry(),
        )
        world.place_character("mizuki", "city_streets")
        graph = world.locations

        def route():
            return plan_route(
                graph,
                lambda l: world.may_enter("mizuki", l),
                "city_streets",
                "kamiyama_high",
            )

        self.assertIsNone(route())
        world._grant_location("mizuki", "kamiyama_high", "student")
        self.assertEqual(route().total_minutes, 3)

    def test_an_origin_the_character_could_not_enter_does_not_block_leaving(self):
        # 起点不问：即使谓词对起点说"不能进"，也照样走得出去。
        route = plan_route(
            build_default_location_graph(),
            lambda l: l != "ena_home",
            "ena_home",
            "city_streets",
        )
        self.assertEqual(route.nodes, ("ena_home", "city_streets"))


class PurityTests(unittest.TestCase):
    def test_the_graph_is_not_modified(self):
        graph = build_default_location_graph()
        before = graph.to_dict()
        plan_route(graph, _open, "mizuki_home_room", "clothing_store_floor")
        self.assertEqual(graph.to_dict(), before)

    def test_unknown_endpoints_are_refused(self):
        graph = _diamond()
        for from_id, to_id in (("atlantis", "d"), ("a", "atlantis"), (None, "d")):
            with self.subTest(from_id=from_id, to_id=to_id):
                with self.assertRaises(RoutingError):
                    plan_route(graph, _open, from_id, to_id)


if __name__ == "__main__":
    unittest.main()
