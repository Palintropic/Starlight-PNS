# tests/test_location_graph.py — 语义位置图与频道表的结构不变量。
#
# 运行: python -m unittest tests.test_location_graph -v
import unittest

from pns.models.channel import Channel, ChannelRegistry, ChannelRegistryError
from pns.models.location import (
    Connection,
    Location,
    LocationGraph,
    LocationGraphError,
    LocationKind,
)
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph
from pns.world.scene_compat import SCENE_WORLD_MAP
from pns.world.scenes import SCENES


def _loc(location_id, **kwargs):
    kwargs.setdefault("name", location_id)
    return Location(location_id=location_id, **kwargs)


class LocationGraphValidationTests(unittest.TestCase):
    def test_rejects_duplicate_location_ids(self):
        with self.assertRaises(LocationGraphError):
            LocationGraph([_loc("room"), _loc("room")])

    def test_rejects_empty_location_id(self):
        with self.assertRaises(LocationGraphError):
            LocationGraph([_loc("")])

    def test_rejects_dangling_parent(self):
        with self.assertRaises(LocationGraphError):
            LocationGraph([_loc("room", parent_id="no_such_building")])

    def test_rejects_self_parent(self):
        with self.assertRaises(LocationGraphError):
            LocationGraph([_loc("room", parent_id="room")])

    def test_rejects_parent_cycle(self):
        with self.assertRaises(LocationGraphError):
            LocationGraph([_loc("a", parent_id="b"), _loc("b", parent_id="a")])

    def test_rejects_dangling_connection(self):
        with self.assertRaises(LocationGraphError):
            LocationGraph([_loc("a", connections=(Connection("nowhere"),))])

    def test_rejects_self_connection(self):
        with self.assertRaises(LocationGraphError):
            LocationGraph([_loc("a", connections=(Connection("a"),))])

    def test_rejects_negative_travel_time(self):
        with self.assertRaises(LocationGraphError):
            LocationGraph(
                [
                    _loc("a", connections=(Connection("b", travel_minutes=-1),)),
                    _loc("b"),
                ]
            )

    def test_unknown_lookup_is_an_explicit_error(self):
        graph = LocationGraph([_loc("a")])
        with self.assertRaises(LocationGraphError):
            graph.get("b")
        self.assertFalse(graph.has("b"))
        self.assertIn("a", graph)


class LocationGraphQueryTests(unittest.TestCase):
    def setUp(self):
        self.graph = LocationGraph(
            [
                _loc("city", kind=LocationKind.REGION),
                _loc(
                    "house",
                    kind=LocationKind.BUILDING,
                    parent_id="city",
                    connections=(Connection("street", travel_minutes=5),),
                ),
                _loc("room", kind=LocationKind.ROOM, parent_id="house"),
                _loc(
                    "street",
                    kind=LocationKind.OUTDOOR,
                    parent_id="city",
                    connections=(Connection("house", travel_minutes=5),),
                ),
            ]
        )

    def test_ancestors_walk_up_the_containment_chain(self):
        self.assertEqual(self.graph.ancestors("room"), ["house", "city"])
        self.assertEqual(self.graph.ancestors("city"), [])

    def test_containment_covers_self_and_ancestors(self):
        self.assertTrue(self.graph.contains_location("house", "room"))
        self.assertTrue(self.graph.contains_location("city", "room"))
        self.assertTrue(self.graph.contains_location("room", "room"))
        self.assertFalse(self.graph.contains_location("street", "room"))

    def test_travel_time_is_only_reported_for_direct_connections(self):
        self.assertEqual(self.graph.travel_minutes("house", "street"), 5)
        self.assertIsNone(self.graph.travel_minutes("room", "street"))
        self.assertEqual(self.graph.neighbors("house"), ["street"])

    def test_serialization_round_trip_preserves_structure(self):
        restored = LocationGraph.from_dict(self.graph.to_dict())
        self.assertEqual(restored.to_dict(), self.graph.to_dict())
        self.assertEqual(restored.get("room").parent_id, "house")

    def test_to_dict_does_not_leak_mutable_internals(self):
        graph = LocationGraph([_loc("a", access={"public": True})])
        payload = graph.to_dict()
        payload["a"]["access"]["public"] = False
        self.assertTrue(graph.get("a").access["public"])


class ChannelRegistryTests(unittest.TestCase):
    def test_rejects_duplicate_and_empty_channel_ids(self):
        with self.assertRaises(ChannelRegistryError):
            ChannelRegistry([Channel("c", "C"), Channel("c", "C")])
        with self.assertRaises(ChannelRegistryError):
            ChannelRegistry([Channel("", "C")])

    def test_unknown_lookup_is_an_explicit_error(self):
        registry = ChannelRegistry([Channel("c", "C")])
        with self.assertRaises(ChannelRegistryError):
            registry.get("other")
        self.assertTrue(registry.has("c"))

    def test_serialization_round_trip(self):
        registry = build_default_channel_registry()
        restored = ChannelRegistry.from_dict(registry.to_dict())
        self.assertEqual(restored.to_dict(), registry.to_dict())


class DefaultRegistryTests(unittest.TestCase):
    def test_default_graph_validates_and_uses_stable_ids(self):
        graph = build_default_location_graph()
        graph.validate()
        for location in graph:
            self.assertEqual(location.location_id, location.location_id.strip())
            self.assertTrue(location.location_id.isascii())

    def test_default_graph_instances_are_independent(self):
        first = build_default_location_graph()
        second = build_default_location_graph()

        self.assertIsNot(first, second)
        self.assertIsNot(first.get("tokyo"), second.get("tokyo"))
        first.get("tokyo").perception["session_only"] = True
        self.assertNotIn("session_only", second.get("tokyo").perception)

    def test_nightcord_is_a_channel_and_not_a_location(self):
        self.assertTrue(build_default_channel_registry().has("nightcord"))
        self.assertFalse(build_default_location_graph().has("nightcord"))

    def test_every_legacy_scene_resolves_to_seeded_locations_and_channels(self):
        graph = build_default_location_graph()
        channels = build_default_channel_registry()
        for scene_id in SCENES:
            with self.subTest(scene=scene_id):
                mapping = SCENE_WORLD_MAP[scene_id]
                self.assertTrue(graph.has(mapping.default_location_id))
                for location_id in mapping.character_locations.values():
                    self.assertTrue(graph.has(location_id))
                for channel_id in mapping.channel_ids:
                    self.assertTrue(channels.has(channel_id))


if __name__ == "__main__":
    unittest.main()
