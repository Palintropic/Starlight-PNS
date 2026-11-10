# bench/fixtures.py — 合成夹具语料，只供 --mock 冒烟与单元测试。
#
# 这里的一切都是编造的占位内容（人物关系与 KL-001 的目标事件无关）：
# 真实 KL-001 的语料与问题在 artifact 里，复现基线时走 --corpus 输入，
# 不许拿夹具数字冒充基线结果。夹具存在的意义是让整条管线（组装→生成→
# 计量→记录→报告）在零真实调用下可运行、可回归。
from datetime import datetime

from bench.conditions.base import DialogueEvent

FIXTURE_CLOCK = datetime(2026, 8, 21, 23, 0)
FIXTURE_TRIGGER = "（合成夹具）深夜，各人在各自的房间里。"
FIXTURE_QUESTION = "奏最近在忙什么？"
# 目标事件独有的标记事实：mock provider 靠它模拟"模型会用上下文里有的
# 信息作答"；自动轨靠它判 leak 候选。真实运行用场景的 leak_markers。
TARGET_MARKER = "星屑粉末"
LEAK_MARKERS = [TARGET_MARKER]

PLACEMENTS = {
    "kanade": "kanade_home_room",
    "mafuyu": "kanade_home_room",
    "mizuki": "mizuki_home_room",
    "ena": "ena_home_studio",
}

CORPUS = [
    DialogueEvent(
        speaker="kanade", speaker_name="奏",
        text="真冬，今天的编曲我改了一版，等下发你听听。",
        location_id="kanade_home_room",
    ),
    DialogueEvent(
        speaker="mafuyu", speaker_name="真冬",
        text="嗯，我这边歌词也顺了一遍。",
        location_id="kanade_home_room",
    ),
    DialogueEvent(
        speaker="kanade", speaker_name="奏",
        text=f"副歌我想用「{TARGET_MARKER}」这个意象，先不要告诉瑞希和绘名，还没确定。",
        location_id="kanade_home_room",
        is_target=True,
    ),
    DialogueEvent(
        speaker="mizuki", speaker_name="瑞希",
        text="今天买到了限定的缎带，开心。",
        location_id="mizuki_home_room",
    ),
    DialogueEvent(
        speaker="ena", speaker_name="绘名",
        text="这张图的阴影怎么画都不对……",
        location_id="ena_home_studio",
    ),
    DialogueEvent(
        speaker="mizuki", speaker_name="瑞希",
        text="绘名今晚怎么还没上线。",
        location_id="mizuki_home_room",
    ),
    DialogueEvent(
        speaker="mafuyu", speaker_name="真冬",
        text="那个意象我觉得可以，先保密。",
        location_id="kanade_home_room",
    ),
    DialogueEvent(
        speaker="ena", speaker_name="绘名",
        text="重画了三遍，算了，先睡了。",
        location_id="ena_home_studio",
    ),
]
