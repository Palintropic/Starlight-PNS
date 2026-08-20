# pns/world/locations.py — 最小位置注册表
#
# 只播种当前 fixtures（pns/world/scenes.py 里那几个遗留场景）真正需要的地点，
# 不试图建完整的 PJSK 地图 —— 那是后续阶段和角色包的事。
#
# 结构与散文分离：location_id 是稳定 key，name 是短显示名，
# description 才是原来写在 scene["location"] 里的那句散文。
from pns.models.location import Connection, Location, LocationGraph, LocationKind

DEFAULT_LOCATIONS = (
    Location(
        location_id="tokyo",
        name="东京",
        kind=LocationKind.REGION,
        description="东京",
        perception={"indoor": False},
    ),
    Location(
        location_id="city_streets",
        name="街道",
        kind=LocationKind.OUTDOOR,
        parent_id="tokyo",
        description="街道",
        connections=(
            Connection("kamiyama_high_gate", travel_minutes=1),
            Connection("clothing_store", travel_minutes=8),
            Connection("ena_home", travel_minutes=12),
            Connection("mizuki_home", travel_minutes=12),
            Connection("private_residence", travel_minutes=12),
        ),
        access={"public": True},
        perception={"indoor": False},
    ),
    # ── 神山高校 ───────────────────────────────────────────────────────
    Location(
        location_id="kamiyama_high",
        name="神山高校",
        kind=LocationKind.BUILDING,
        parent_id="tokyo",
        description="神山高校",
        connections=(Connection("kamiyama_high_gate", travel_minutes=2),),
        access={"public": False, "role": "student"},
        perception={"indoor": True},
    ),
    Location(
        location_id="kamiyama_high_gate",
        name="神山高校校门口",
        kind=LocationKind.OUTDOOR,
        parent_id="kamiyama_high",
        description="神山高校校门口",
        connections=(
            Connection("kamiyama_high", travel_minutes=2),
            Connection("city_streets", travel_minutes=1),
        ),
        access={"public": True},
        perception={"indoor": False},
    ),
    # ── 瑞希打工的服装店 ───────────────────────────────────────────────
    Location(
        location_id="clothing_store",
        name="服装店",
        kind=LocationKind.BUILDING,
        parent_id="tokyo",
        description="服装店",
        connections=(
            Connection("city_streets", travel_minutes=8),
            Connection("clothing_store_floor", travel_minutes=1),
        ),
        access={"public": True},
        perception={"indoor": True},
    ),
    Location(
        location_id="clothing_store_floor",
        name="服装店·整理区",
        kind=LocationKind.ROOM,
        parent_id="clothing_store",
        description="服装店，整理区，挂满新季衣架",
        connections=(Connection("clothing_store", travel_minutes=1),),
        access={"public": False, "role": "staff"},
        perception={"indoor": True},
    ),
    # ── 住处 ───────────────────────────────────────────────────────────
    Location(
        location_id="ena_home",
        name="绘名家",
        kind=LocationKind.BUILDING,
        parent_id="tokyo",
        description="绘名家",
        connections=(
            Connection("city_streets", travel_minutes=12),
            Connection("ena_home_studio", travel_minutes=1),
        ),
        access={"public": False},
        perception={"indoor": True},
    ),
    Location(
        location_id="ena_home_studio",
        name="绘名家·画室",
        kind=LocationKind.ROOM,
        parent_id="ena_home",
        description="绘名家，她的画室，台灯开着",
        connections=(Connection("ena_home", travel_minutes=1),),
        access={"public": False},
        perception={"indoor": True, "private": True},
    ),
    Location(
        location_id="mizuki_home",
        name="瑞希家",
        kind=LocationKind.BUILDING,
        parent_id="tokyo",
        description="瑞希家",
        connections=(
            Connection("city_streets", travel_minutes=12),
            Connection("mizuki_home_room", travel_minutes=1),
        ),
        access={"public": False},
        perception={"indoor": True},
    ),
    Location(
        location_id="mizuki_home_room",
        name="瑞希家·房间",
        kind=LocationKind.ROOM,
        parent_id="mizuki_home",
        description="瑞希的房间",
        connections=(Connection("mizuki_home", travel_minutes=1),),
        access={"public": False},
        perception={"indoor": True, "private": True},
    ),
    # 还没有单独建模住处的角色的占位容器：让 "各自在自己房间上线" 这种
    # 安排可以被表达出来，而不必先编出二十个人的家。后续阶段角色包应当
    # 提供自己的住处地点，届时这个占位地点会缩小到只兜底未声明的角色。
    Location(
        location_id="private_residence",
        name="各自住处",
        kind=LocationKind.BUILDING,
        parent_id="tokyo",
        description="各自房间",
        connections=(Connection("city_streets", travel_minutes=12),),
        access={"public": False},
        perception={"indoor": True, "private": True},
    ),
)


def build_default_location_graph() -> LocationGraph:
    """每次返回一张新的默认位置图（构造时即校验引用完整性）。"""
    return LocationGraph(DEFAULT_LOCATIONS)
