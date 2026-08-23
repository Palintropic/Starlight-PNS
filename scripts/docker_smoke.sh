#!/usr/bin/env bash
# scripts/docker_smoke.sh — 对**真的**镜像和容器跑一遍 DEPLOY-1 的验收条件。
#
# 它证的是自动化测试证不了的那一半：镜像层里有什么、容器重建之后卷里还剩
# 什么、健康检查在真的编排下走不走得通、容器名冲突会不会响亮失败。
#
# 它不碰仓库里的 .env，也不碰默认的 compose 项目：整个过程用一个独立的项目名
# （pns-smoke）、一个临时 env 文件、一组独立的卷，结束时全部删掉。
#
# provider 指向一个不可路由的地址：这个脚本从头到尾不该发生任何一次真实模型
# 调用，指错地方比"相信它不会调用"可靠。
#
# 用法： scripts/docker_smoke.sh          （跑完自动清理）
#        KEEP=1 scripts/docker_smoke.sh   （留下容器和卷供人工查看）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="pns-smoke"
IMAGE="starlight-pns:smoke"
PORT="${SMOKE_PORT:-17860}"
BASE="http://127.0.0.1:${PORT}"

ADMIN_TOKEN="SMOKE-ADMIN-CANARY-0011223344556677889900aabbccddee"
KEY_CANARY="SMOKE-MODEL-CANARY-ffeeddccbbaa00998877665544332211"

WORK="$(mktemp -d)"
COOKIES="${WORK}/cookies.txt"
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }

compose() {
  docker compose -p "$PROJECT" -f "${REPO_ROOT}/compose.yaml" -f "${WORK}/override.yaml" "$@"
}

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then
    echo "KEEP=1：保留 ${PROJECT} 的容器与卷，临时文件在 ${WORK}"
    return
  fi
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker rm -f "${PROJECT}-duplicate" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cat > "${WORK}/smoke.env" <<ENV
PNS_ADMIN_TOKEN=${ADMIN_TOKEN}
PNS_API_KEY_NAME=SMOKE_KEY
SMOKE_KEY=${KEY_CANARY}
API_FORMAT=openai
BASE_URL=http://127.0.0.1:9/v1
MODEL=smoke-model
PNS_SESSION_COOKIE_SECURE=false
ENV

cat > "${WORK}/override.yaml" <<OVERRIDE
services:
  app:
    image: ${IMAGE}
    container_name: ${PROJECT}
    env_file:
      - ${WORK}/smoke.env
    ports:
      - "127.0.0.1:${PORT}:7860"
OVERRIDE

api() {  # api METHOD PATH [BODY]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -o "${WORK}/body" -w '%{http_code}' -b "$COOKIES" -c "$COOKIES" \
      -X "$method" -H 'Content-Type: application/json' -d "$body" "${BASE}${path}"
  else
    curl -sS -o "${WORK}/body" -w '%{http_code}' -b "$COOKIES" -c "$COOKIES" \
      -X "$method" "${BASE}${path}"
  fi
}

anon() {  # 不带 Cookie 的请求
  curl -sS -o "${WORK}/body" -w '%{http_code}' -X "${1}" "${BASE}${2}"
}

body() { cat "${WORK}/body"; }
field() { python3 -c "import json,sys;print(json.load(open('${WORK}/body')).get('$1'))"; }

# ── A1：构建与启动 ─────────────────────────────────────────────────────
step "A1  构建镜像并通过 Compose 启动"
docker build -t "$IMAGE" "$REPO_ROOT" >"${WORK}/build.log" 2>&1 \
  && ok "镜像构建成功" || { bad "镜像构建失败（见 ${WORK}/build.log）"; tail -20 "${WORK}/build.log"; exit 1; }

check "镜像不以 root 运行" \
  '[ "$(docker image inspect "$IMAGE" --format "{{.Config.User}}")" = "10001:10001" ]'
check "镜像里没有 .env" \
  '! docker run --rm --entrypoint python "$IMAGE" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\"/app/.env\").exists() else 1)"'
check "镜像里没有 tests/" \
  '! docker run --rm --entrypoint python "$IMAGE" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\"/app/tests\").exists() else 1)"'
check "docker history 里没有凭据" \
  '! docker history --no-trunc "$IMAGE" | grep -q "$ADMIN_TOKEN"'

compose up -d >/dev/null 2>&1 || { bad "compose up 失败"; compose logs | tail -30; exit 1; }

printf '  等待健康检查'
healthy=0
for _ in $(seq 1 60); do
  state="$(docker inspect --format '{{.State.Health.Status}}' "$PROJECT" 2>/dev/null || echo none)"
  if [ "$state" = "healthy" ]; then healthy=1; break; fi
  printf '.'; sleep 2
done
printf '\n'
[ "$healthy" = "1" ] && ok "容器进入 healthy" || { bad "容器没有变 healthy"; compose logs | tail -40; }

check "未鉴权也能拿到 /readyz" '[ "$(anon GET /readyz)" = "200" ]'
check "Dashboard 首页由同一个源提供" '[ "$(anon GET /)" = "200" ]'
check "前端包里没有凭据" \
  '! docker exec "$PROJECT" sh -c "grep -rq \"$ADMIN_TOKEN\" /app/dashboard/dist"'

# ── A4：特权端点拒绝无凭据请求 ─────────────────────────────────────────
step "A4  未授权请求不能执行管理操作"
for path in /api/persistent-worlds /api/config /api/config/reload /openapi.json; do
  check "GET ${path} 无凭据 → 401" '[ "$(anon GET '"$path"')" = "401" ]'
done
# 一次真的会改状态的写请求：证的是"拒绝发生在任何变更之前"，不只是读被挡住。
CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"world_id":"unauthorized","scene":"nightcord","characters":["mizuki"]}' \
  "${BASE}/api/persistent-worlds")"
check "POST /api/persistent-worlds 无凭据 → 401" '[ "'"$CODE"'" = "401" ]'

CODE="$(curl -sS -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer wrong' "${BASE}/api/config")"
check "错凭据 → 401" '[ "'"$CODE"'" = "401" ]'

check "被拒绝的创建没有在卷上留下世界目录" \
  '! docker exec "$PROJECT" sh -c "test -e /app/data/worlds/unauthorized"'

step "A1  登录之后管理面可用"
check "登录成功" '[ "$(api POST /api/auth/login "{\"token\":\"${ADMIN_TOKEN}\"}")" = "200" ]'
check "会话 Cookie 是 HttpOnly" 'grep -q "#HttpOnly_" "$COOKIES"'
check "带会话可读世界列表" '[ "$(api GET /api/persistent-worlds)" = "200" ]'

# ── A2：重建容器不丢世界 ───────────────────────────────────────────────
step "A2  重建容器不丢世界"
code="$(api POST /api/persistent-worlds '{"world_id":"smoke","scene":"nightcord","characters":["mizuki","ena"]}')"
check "建世界 → 201" '[ "'"$code"'" = "201" ]'
api POST /api/persistent-worlds/smoke/activity '{"character_id":"mizuki","activity":"drawing"}' >/dev/null
api GET /api/persistent-worlds/smoke >/dev/null
REVISION="$(field revision)"; CLOCK="$(field clock)"
echo "  记下：revision=${REVISION} clock=${CLOCK}"

check "活数据不在镜像层上（无卷启动时 /app/data 是空的）" \
  '[ -z "$(docker run --rm --entrypoint sh "$IMAGE" -c "ls -A /app/data")" ]'

compose down >/dev/null 2>&1
docker build -t "$IMAGE" "$REPO_ROOT" >/dev/null 2>&1
compose up -d >/dev/null 2>&1
for _ in $(seq 1 60); do
  [ "$(docker inspect --format '{{.State.Health.Status}}' "$PROJECT" 2>/dev/null || echo none)" = "healthy" ] && break
  sleep 2
done
rm -f "$COOKIES"
api POST /api/auth/login "{\"token\":\"${ADMIN_TOKEN}\"}" >/dev/null
code="$(api POST /api/persistent-worlds/smoke/restore)"
check "重建后恢复 → 200" '[ "'"$code"'" = "200" ]'
check "revision 没有回退" '[ "$(field revision)" -ge "'"$REVISION"'" ]'
check "世界时钟一致" '[ "$(field clock)" = "'"$CLOCK"'" ]'

# ── A3：重启不会自动开始模型调用 ───────────────────────────────────────
step "A3  重启之后没有自动模型调用"
sleep 5
api GET /api/persistent-worlds/smoke >/dev/null
check "驱动没有自己跑起来" \
  'python3 -c "import json;d=json.load(open(\"${WORK}/body\"));a=d.get(\"autonomy\");raise SystemExit(0 if a is None or a[\"state\"]==\"stopped\" else 1)"'
check "日志里没有出现过模型调用错误（provider 指向不可路由地址）" \
  '! compose logs 2>/dev/null | grep -qi "9/v1"'

# ── A8：两个写者 ───────────────────────────────────────────────────────
step "A8  第二个应用容器不能静默共写"
if docker run -d --name "$PROJECT" --env-file "${WORK}/smoke.env" "$IMAGE" >/dev/null 2>"${WORK}/dup.log"; then
  bad "同名容器竟然起来了"
  docker rm -f "$PROJECT" >/dev/null 2>&1 || true
else
  grep -qi "already in use" "${WORK}/dup.log" \
    && ok "同名容器被响亮拒绝（Compose 层单写者）" \
    || { bad "第二个容器失败了，但不是因为名字冲突"; cat "${WORK}/dup.log"; }
fi

# ── A6：停机 ───────────────────────────────────────────────────────────
step "A6  SIGTERM 走文档写明的生命周期"
compose stop >/dev/null 2>&1
EXIT_CODE="$(docker inspect --format '{{.State.ExitCode}}' "$PROJECT")"
check "退出码是 143（128+SIGTERM，docker stop 的正常形状）" '[ "'"$EXIT_CODE"'" = "143" ]'
check "关闭报告如实说明世界已干净关闭" \
  'compose logs 2>/dev/null | grep -q "已干净关闭"'
check "没有把一次失败的收尾说成干净关闭" \
  '! compose logs 2>/dev/null | grep -q "\*\*没有\*\*干净关闭"'

# ── A5：日志里没有凭据 ─────────────────────────────────────────────────
step "A5  捕获到的日志里没有凭据"
compose logs > "${WORK}/logs.txt" 2>&1 || true
check "日志里没有管理凭据" '! grep -q "$ADMIN_TOKEN" "${WORK}/logs.txt"'
check "日志里没有模型凭据" '! grep -q "$KEY_CANARY" "${WORK}/logs.txt"'
check "日志仍然有用（能看到世界 id）" 'grep -q "smoke" "${WORK}/logs.txt"'

printf '\n\033[1m结果\033[0m  通过 %d 项，失败 %d 项\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
