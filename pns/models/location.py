# pns/models/location.py — 物理位置领域模型
#
# Location 是持久世界里的一个地点实体，用稳定的 location_id 标识，
# 与散文地名（"绘名家，她的画室，台灯开着"）解耦：散文只是 description，
# 不参与任何查找。线上频道不属于这里，见 pns/models/channel.py。
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, Iterator, List, Optional, Tuple


class LocationKind(str, Enum):
    """地点的粒度/类型。"""

    REGION = "region"
    BUILDING = "building"
    ROOM = "room"
    OUTDOOR = "outdoor"
    TRANSIT = "transit"


@dataclass(frozen=True)
class Connection:
    """从某个地点出发可直接到达的一条通路。"""

    to_id: str
    travel_minutes: int = 0
    mode: str = "walk"

    def to_dict(self) -> Dict:
        return {
            "to_id": self.to_id,
            "travel_minutes": self.travel_minutes,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "Connection":
        return cls(
            to_id=payload["to_id"],
            travel_minutes=int(payload.get("travel_minutes", 0)),
            mode=payload.get("mode", "walk"),
        )


@dataclass(frozen=True)
class Location:
    """一个物理地点。

    access / perception 只放静态元数据（是否公开、室内外、可见可闻范围等）；
    随会话变化的环境状态属于 WorldState.location_state，不写在这里。
    """

    location_id: str
    name: str
    kind: LocationKind = LocationKind.ROOM
    parent_id: Optional[str] = None
    description: str = ""
    connections: Tuple[Connection, ...] = ()
    access: Dict = field(default_factory=dict)
    perception: Dict = field(default_factory=dict)

    @property
    def display(self) -> str:
        """给人看的地点描述，缺省回落到 name。"""
        return self.description or self.name

    def to_dict(self) -> Dict:
        return {
            "location_id": self.location_id,
            "name": self.name,
            "kind": self.kind.value,
            "parent_id": self.parent_id,
            "description": self.description,
            "connections": [c.to_dict() for c in self.connections],
            "access": deepcopy(self.access),
            "perception": deepcopy(self.perception),
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "Location":
        return cls(
            location_id=payload["location_id"],
            name=payload["name"],
            kind=LocationKind(payload.get("kind", LocationKind.ROOM.value)),
            parent_id=payload.get("parent_id"),
            description=payload.get("description", ""),
            connections=tuple(
                Connection.from_dict(c) for c in payload.get("connections", [])
            ),
            access=deepcopy(payload.get("access", {})),
            perception=deepcopy(payload.get("perception", {})),
        )


class LocationGraphError(ValueError):
    """位置图自身不自洽（重复 ID、悬空 parent/connection、父级成环等）。"""


class LocationGraph:
    """语义位置图。

    只负责地点之间的静态结构（从属、连通、通行时间），不持有任何角色状态。
    """

    def __init__(self, locations: Iterable[Location] = ()):
        self._locations: Dict[str, Location] = {}
        for location in locations:
            self.add(location)
        self.validate()

    def add(self, location: Location) -> None:
        if not location.location_id:
            raise LocationGraphError("location_id 不能为空")
        if location.location_id in self._locations:
            raise LocationGraphError(f"重复的 location_id: {location.location_id}")
        # A graph owns its static metadata.  Location is a frozen dataclass, but
        # access/perception are nested mutable dictionaries; retaining the
        # caller's object would let two graphs (and therefore two sessions)
        # mutate each other's rules through those dictionaries.
        owned_location = Location.from_dict(location.to_dict())
        self._locations[owned_location.location_id] = owned_location

    def __contains__(self, location_id: object) -> bool:
        return location_id in self._locations

    def __iter__(self) -> Iterator[Location]:
        return iter(self._locations.values())

    def __len__(self) -> int:
        return len(self._locations)

    def has(self, location_id: str) -> bool:
        return location_id in self._locations

    def get(self, location_id: str) -> Location:
        try:
            return self._locations[location_id]
        except KeyError:
            raise LocationGraphError(f"未知的 location_id: {location_id}") from None

    def ids(self) -> List[str]:
        return list(self._locations)

    def validate(self) -> None:
        """检查引用完整性；构造完成后以及外部批量 add() 之后都应该调用。"""
        for location in self._locations.values():
            parent_id = location.parent_id
            if parent_id is not None:
                if parent_id == location.location_id:
                    raise LocationGraphError(
                        f"地点 '{location.location_id}' 的 parent 指向自己"
                    )
                if parent_id not in self._locations:
                    raise LocationGraphError(
                        f"地点 '{location.location_id}' 的 parent '{parent_id}' 不存在"
                    )
            for connection in location.connections:
                if connection.to_id == location.location_id:
                    raise LocationGraphError(
                        f"地点 '{location.location_id}' 连向了自己"
                    )
                if connection.to_id not in self._locations:
                    raise LocationGraphError(
                        f"地点 '{location.location_id}' 连向了不存在的 "
                        f"'{connection.to_id}'"
                    )
                if connection.travel_minutes < 0:
                    raise LocationGraphError(
                        f"地点 '{location.location_id}' → '{connection.to_id}' "
                        f"的通行时间不能为负"
                    )
        for location_id in self._locations:
            self.ancestors(location_id)

    def ancestors(self, location_id: str) -> List[str]:
        """自底向上返回所有祖先 ID；父级成环时报错。"""
        chain: List[str] = []
        seen = {location_id}
        current = self.get(location_id).parent_id
        while current is not None:
            if current in seen:
                raise LocationGraphError(f"地点父级关系成环: {location_id}")
            seen.add(current)
            chain.append(current)
            current = self.get(current).parent_id
        return chain

    def contains_location(self, container_id: str, location_id: str) -> bool:
        """container_id 是否是 location_id 本身或它的祖先。"""
        if container_id == location_id:
            return True
        return container_id in self.ancestors(location_id)

    def neighbors(self, location_id: str) -> List[str]:
        return [c.to_id for c in self.get(location_id).connections]

    def travel_minutes(self, from_id: str, to_id: str) -> Optional[int]:
        """直连通行时间；不直连返回 None（本阶段不做寻路）。"""
        self.get(to_id)
        for connection in self.get(from_id).connections:
            if connection.to_id == to_id:
                return connection.travel_minutes
        return None

    def to_dict(self) -> Dict:
        return {lid: loc.to_dict() for lid, loc in self._locations.items()}

    @classmethod
    def from_dict(cls, payload: Dict) -> "LocationGraph":
        return cls(Location.from_dict(entry) for entry in payload.values())
