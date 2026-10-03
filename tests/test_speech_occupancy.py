# tests/test_speech_occupancy.py — 台词的在场名单在提交边界核对（WEB-3 审查 F2）。
#
# 世界页靠地点档 / 频道档台词的 participants 判断"独自 / 当面 / 线上"。说话人在名单里
# 不能证明名单是全的，所以提交时按权威状态逐人核对：
#   1. 地点档：恰好是此刻在那个节点的人；说话人必须在那里；不能同时挂频道。
#   2. 频道档：恰好是此刻频道里的人；说话人必须在频道里。
#   3. 点名档（private / participant）不受此约束：那一档的名单是被点名者。
#   4. 世界第一次在有这条核对的代码下绑定时，记下从第几条事件起受核对，之后不改。
#
# 运行: python -m unittest discover -s tests -p test_speech_occupancy.py
import unittest
from datetime import datetime

from grants_support import grant_everything
from pns.models.event import Event, EventScope, EventType
from pns.models.session import SessionState
from pns.models.world_state import WorldState
from pns.runtime.autonomy.audit import ScriptedAuditor
from pns.runtime.event_commit import (
    SPEECH_OCCUPANCY_CHECKED_FROM,
    EventCommitError,
    commit_session_event,
)
from pns.runtime.persistence.lifecycle import RuntimeAdapters
from pns.world.channels import build_default_channel_registry
from pns.world.locations import build_default_location_graph

CLOCK = datetime(2026, 10, 3, 21, 0)


def _state():
    world = WorldState(
        clock=CLOCK,
        locations=build_default_location_graph(),
        channels=build_default_channel_registry(),
    )
    grant_everything(world)
    world.place_character("kanade", "kanade_home")
    world.place_character("mafuyu", "kanade_home")
    world.place_character("mizuki", "mizuki_home_room")
    world.place_character("ena", "ena_home_studio")
    world.join_channel("mizuki", "nightcord")
    world.join_channel("ena", "nightcord")
    state = SessionState(
        session_id="s", scene="gate", characters=["kanade", "mafuyu", "mizuki", "ena"]
    )
    state.attach_world_state(world)
    return state


def _line(event_id="e", **fields):
    base = {
        "event_id": event_id,
        "type": EventType.DIALOGUE_SPOKEN,
        "occurred_at": CLOCK,
        "scope": EventScope.LOCATION,
        "actor_id": "kanade",
        "participants": ("kanade", "mafuyu"),
        "location_id": "kanade_home",
        "payload": {"text": "要不要先吃点东西？"},
    }
    base.update(fields)
    return Event(**base)


class LocationSpeechTests(unittest.TestCase):
    def test_the_exact_occupants_are_accepted(self):
        state = _state()
        commit_session_event(state, _line())
        self.assertEqual(len(state.events), 1)

    def test_a_list_that_leaves_someone_out_is_rejected(self):
        # 审查 F2 的反例：两人同处，名单只写说话人，原来会被页面读成"独自"。
        state = _state()
        with self.assertRaises(EventCommitError):
            commit_session_event(state, _line(participants=("kanade",)))
        self.assertEqual(len(state.events), 0)

    def test_a_list_that_adds_someone_elsewhere_is_rejected(self):
        state = _state()
        with self.assertRaises(EventCommitError):
            commit_session_event(state, _line(participants=("kanade", "mafuyu", "mizuki")))

    def test_the_speaker_must_be_there(self):
        state = _state()
        with self.assertRaises(EventCommitError):
            commit_session_event(
                state, _line(actor_id="mizuki", participants=("kanade", "mafuyu", "mizuki"))
            )

    def test_location_speech_cannot_also_claim_a_channel(self):
        state = _state()
        with self.assertRaises(EventCommitError):
            commit_session_event(state, _line(channel_id="nightcord"))

    def test_alone_means_nobody_else_is_on_that_node(self):
        state = _state()
        commit_session_event(
            state,
            _line(actor_id="mizuki", participants=("mizuki",), location_id="mizuki_home_room"),
        )
        self.assertEqual(len(state.events), 1)


class ChannelSpeechTests(unittest.TestCase):
    def _message(self, **fields):
        base = {
            "type": EventType.MESSAGE_SENT,
            "scope": EventScope.CHANNEL,
            "actor_id": "ena",
            "participants": ("ena", "mizuki"),
            "location_id": None,
            "channel_id": "nightcord",
            "payload": {"text": "我先上线了"},
        }
        base.update(fields)
        return _line(**base)

    def test_the_exact_members_online_are_accepted(self):
        state = _state()
        commit_session_event(state, self._message())
        self.assertEqual(len(state.events), 1)

    def test_a_partial_member_list_is_rejected(self):
        state = _state()
        with self.assertRaises(EventCommitError):
            commit_session_event(state, self._message(participants=("ena",)))

    def test_a_speaker_outside_the_channel_is_rejected(self):
        state = _state()
        with self.assertRaises(EventCommitError):
            commit_session_event(
                state, self._message(actor_id="kanade", participants=("ena", "kanade", "mizuki"))
            )


class NamedSpeechTests(unittest.TestCase):
    def test_participant_scope_lists_who_was_addressed_not_who_was_there(self):
        # 审查 F1 的事件形状：点名档仍可提交；页面那一侧不把它当在场快照（前端测试覆盖）。
        state = _state()
        commit_session_event(
            state,
            _line(scope=EventScope.PARTICIPANT, participants=("kanade", "mizuki")),
        )
        self.assertEqual(len(state.events), 1)


class CheckedFromMarkerTests(unittest.TestCase):
    def _bind(self, state):
        RuntimeAdapters(auditor=ScriptedAuditor()).bind(state)

    def test_a_world_first_bound_with_the_check_starts_at_its_current_length(self):
        state = _state()
        # 这条台词模拟"旧版本时提交的历史"：直接放进历史，不经过新核对。
        state.events._append(_line("old", participants=("kanade",)))
        self._bind(state)
        self.assertEqual(state.world_state.metadata[SPEECH_OCCUPANCY_CHECKED_FROM], 1)

    def test_a_brand_new_world_is_checked_from_the_start(self):
        state = _state()
        self._bind(state)
        self.assertEqual(state.world_state.metadata[SPEECH_OCCUPANCY_CHECKED_FROM], 0)

    def test_the_marker_is_never_moved_once_recorded(self):
        state = _state()
        state.world_state.metadata[SPEECH_OCCUPANCY_CHECKED_FROM] = 0
        state.events._append(_line("later"))
        self._bind(state)
        self.assertEqual(state.world_state.metadata[SPEECH_OCCUPANCY_CHECKED_FROM], 0)

    def test_the_marker_survives_a_save_and_restore(self):
        state = _state()
        self._bind(state)
        restored = SessionState.from_dict(state.to_dict())
        self.assertEqual(restored.world_state.metadata[SPEECH_OCCUPANCY_CHECKED_FROM], 0)


if __name__ == "__main__":
    unittest.main()
