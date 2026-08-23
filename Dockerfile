# Starlight-PNS 生产镜像
#
# 刻意不写 `# syntax=` 前端指令：那会让每次构建先去拉一个前端镜像，等于给
# 构建加一条网络依赖，而这份 Dockerfile 一个前端专属特性都没用到。
#
# 目标环境：ESXi 上一台独立的 Ubuntu Server 虚拟机，Docker Engine + Compose。
# 不为 WSL、Windows 容器或 Docker Desktop 做任何适配。
#
# 三条构建期边界：
#   1. **构建期没有任何密钥。** 上下文由 .dockerignore 默认全排除、显式放行，
#      `.env` 和所有运行时数据都进不来。凭据只在运行时由 Compose 注入。
#   2. **最终镜像里只有运行时需要的东西。** 没有 .git、没有测试、没有格式化
#      器、没有 node_modules、没有本地存档。前端只有构建产物，构建工具链留在
#      被丢弃的第一阶段里。
#   3. **不以 root 运行。** 应用用户是 10001:10001；`/app/data` 与
#      `/app/history` 在镜像里就归它，具名卷首次挂载会继承这份属主，于是
#      Ubuntu 宿主上不需要任何 chown 步骤。
#
# 基础镜像按 digest 钉死。只钉 tag 的话，"同一份 Dockerfile"在两周后会构建出
# 一个不同的镜像，而回滚要靠的正是"同一份输入构建出同一个东西"。

# ── 阶段 1：构建 Dashboard ──────────────────────────────────────────────
FROM node:22.23.2-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS dashboard

WORKDIR /build

# 先只拷依赖清单：源码改动不会让依赖安装那一层失效。
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

COPY dashboard/ ./
RUN npm run build


# ── 阶段 2：运行时 ──────────────────────────────────────────────────────
FROM python:3.12.14-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134 AS runtime

# PNS_ENV 固化在镜像里，而不是只写在 compose.yaml 上：这是**生产**镜像。
# 就算有人拿 `docker run` 直接跑它、忘了传 PNS_ENV，它也不会退回到那条
# 不鉴权的本地开发路径——那条路径只属于直接 `python scripts/server.py`。
ENV PNS_ENV=production \
    PORT=7860 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 pns \
    && useradd --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin --no-create-home pns

WORKDIR /app

COPY requirements-prod.txt ./
# --timeout/--retries：慢链路上的构建不该因为一次读超时就整个失败重来。
RUN python -m pip install --no-cache-dir --timeout 60 --retries 10 -r requirements-prod.txt

COPY --chown=10001:10001 pns/ ./pns/
COPY --chown=10001:10001 scripts/ ./scripts/
COPY --chown=10001:10001 packs/ ./packs/
COPY --chown=10001:10001 config.yaml ./
COPY --from=dashboard --chown=10001:10001 /build/dist ./dashboard/dist

# 运行时数据的挂载点。镜像里是空目录且归应用用户所有——具名卷首次挂载时
# Docker 会继承这份属主，所以非 root 的进程直接就能写。
RUN mkdir -p /app/data /app/history \
    && chown 10001:10001 /app/data /app/history

USER 10001:10001

EXPOSE 7860

# 判据只有一份，在 scripts/healthcheck.py 里；compose.yaml 调的是同一个文件。
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

# exec 形式：python 就是 PID 1，直接收得到 SIGTERM，uvicorn 的优雅停机因此
# 真的会跑（走 shell 形式的话信号会停在 /bin/sh 上）。
CMD ["python", "scripts/server.py"]
