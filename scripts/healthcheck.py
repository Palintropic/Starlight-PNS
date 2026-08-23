# scripts/healthcheck.py — 容器健康探针
#
# Dockerfile 的 HEALTHCHECK 和 compose.yaml 的 healthcheck 都调用这一个文件，
# 所以"健康怎么判"只有一份定义。两处各写一遍 python -c 的话，改一处忘一处
# 就会出现两个都自称权威的答案。
#
# 它打的是 `/readyz`，那是一条**公开**路径（编排系统没有凭据），而且它没有
# 权威副作用：不调用模型、不推进时间、不重载配置、不获取世界所有权。
# 见 pns/interfaces/health.py。
import os
import sys
import urllib.request

DEFAULT_TIMEOUT = 4.0


def probe(url: str, timeout: float = DEFAULT_TIMEOUT) -> int:
    """0 = 健康。任何异常都算不健康——探针自己绝不吞掉一次失败。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


def url_from_env(env=None) -> str:
    env = os.environ if env is None else env
    port = env.get("PORT", "7860")
    return f"http://127.0.0.1:{port}/readyz"


def main(argv=None) -> int:
    return probe(url_from_env())


if __name__ == "__main__":
    sys.exit(main())
