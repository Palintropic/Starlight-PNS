# pns/runtime/provider_switch.py — 模型线路保存与世界入口的闸门（CFG-1 §5.2）
#
# 世界的 adapters（生成 / 判分 client）只在创建、正式开局、恢复时按当时的
# registry 建一次。所以保存模型设置（PUT / DELETE）必须跟这三个入口互斥：
# 否则一个入口可能读了旧快照、在保存完成之后才登记，世界就带着旧线路开着，
# 而保存那边以为"没有世界开着"。
#
# 互斥只在"切换"和"世界入口"之间：两个世界入口之间照旧可以并发（各自的
# 世界由 P12 的所有权管），所以这是一把共享 / 独占锁——世界入口持共享，
# 切换持独占。两边都不排队：拿不到就立刻抛 ProviderSwitchBusy，接口层翻成
# 409，调用方稍后重试。
#
# 世界入口的持锁范围由调用方保证：从读取 registry、构建 adapters 之前开始，
# 到登记与启动收尾结束（见 WorldControlPlane.create / create_formal / restore）。
#
# 只管本进程：部署是单写者拓扑（compose 里只有一个容器写 data/）。
import threading
from contextlib import contextmanager
from typing import Iterator


class ProviderSwitchBusy(RuntimeError):
    """切换和世界入口撞上了：这次操作什么都没做。"""


class ProviderSwitchGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._world_entries = 0
        self._switching = False

    @property
    def switching(self) -> bool:
        with self._lock:
            return self._switching

    @contextmanager
    def world_entry(self) -> Iterator[None]:
        """创建 / 正式开局 / 恢复一个世界。切换进行中就拒绝。"""
        with self._lock:
            if self._switching:
                raise ProviderSwitchBusy("模型设置正在保存，暂时不能创建或恢复世界，请稍后重试")
            self._world_entries += 1
        try:
            yield
        finally:
            with self._lock:
                self._world_entries -= 1

    @contextmanager
    def switch(self) -> Iterator[None]:
        """保存或恢复默认模型设置。有世界入口正在进行、或另一次切换没完，就拒绝。"""
        with self._lock:
            if self._switching:
                raise ProviderSwitchBusy("另一次模型设置保存还没结束，请稍后重试")
            if self._world_entries:
                raise ProviderSwitchBusy("有世界正在创建或恢复，暂时不能保存模型设置，请稍后重试")
            self._switching = True
        try:
            yield
        finally:
            with self._lock:
                self._switching = False


# 进程内的单例。测试可以给 WorldControlPlane 传自己的实例。
SWITCH_GATE = ProviderSwitchGate()
