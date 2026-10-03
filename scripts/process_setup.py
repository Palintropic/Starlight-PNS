# scripts/process_setup.py — 服务进程和维护脚本共用的进程启动前准备
#
# 只做三件必须最先发生的事：把仓库根目录和 scripts/ 加入 sys.path、加载 .env、
# 把 stdout/stderr 换成会遮蔽凭据的版本。它不 import 应用、不建控制面、不碰磁盘，
# 所以维护脚本可以拿到和服务器一样的配置，却不会顺手起一个 Web 应用。
#
# 遮蔽为什么必须最先装：后面的装配本身就可能失败并打印异常，而那条异常路径
# 正是最容易把凭据带出去的地方。
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _extend_path() -> None:
    for path in (ROOT_DIR, ROOT_DIR / "scripts"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def secret_env_names():
    """哪些环境变量的**值**不许出现在日志里。

    provider 的 key 变量名从 oobe 的 provider 表里取，不写死：新增一个
    provider 就自动进入遮蔽范围，不需要有人记得回来改这里。
    """
    _extend_path()
    from oobe import PROVIDERS
    from pns.interfaces.security import ENV_ADMIN_TOKEN, ENV_BOOTSTRAP_PASSWORD_HASH

    names = [
        ENV_ADMIN_TOKEN,
        # bootstrap 哈希不是明文密码，但它是一份可以拿去离线猜的凭据材料，
        # 没有理由让它出现在任何一条日志里。
        ENV_BOOTSTRAP_PASSWORD_HASH,
        os.environ.get("PNS_API_KEY_NAME", "MIMO_API_KEY"),
    ]
    names.extend(provider["key_name"] for provider in PROVIDERS.values())
    return names


def prepare() -> None:
    """sys.path → .env → 凭据遮蔽，顺序固定。"""
    _extend_path()
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    from pns.interfaces import redaction

    redaction.install(secret_env_names())
