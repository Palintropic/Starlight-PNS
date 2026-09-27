# tests/autonomy_support.py — 自主运行端到端测试共用的小工具。
import threading
import time

from tests.test_mvp_generation import FakeProvider


def wait_for(predicate, timeout=5.0, what="条件"):
    """等一个条件成立。轮询而不是 sleep：等到就立刻往下走，等不到就失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    raise AssertionError(f"等不到{what}（{timeout} 秒）")


def clock_threads():
    """本进程里活着的时钟 worker 线程。"""
    return [t for t in threading.enumerate() if t.name.startswith("pns-clock-")]


class BlockingProvider(FakeProvider):
    """一个可以被卡在"模型调用中"的 provider 替身。

    它让"Stop/关闭时正好有一次模型调用在飞"这件事变成可控的，而不是靠赌时序。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocking = False

    def _create(self, **kwargs):
        if self.blocking:
            self.entered.set()
            # 有界地等：测试挂住比测试失败难查得多。
            self.release.wait(10)
        return super()._create(**kwargs)
