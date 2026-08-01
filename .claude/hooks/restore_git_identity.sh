#!/bin/bash
# SessionStart hook: 每次 CCR（云端容器）开 session 自动把这个仓库的 commit 身份配成
# Koharu-Mizuki，如果那个云端 environment 配了 GPG_SIGNING_KEY（base64 编码的
# ASCII-armored 私钥），顺便恢复 commit 签名。没配 GPG_SIGNING_KEY 时只配身份、跳过
# 签名，不报错。
#
# 本地机器（非容器）直接跳过 —— 本地身份由用户自己的 git config 管理，这个 hook
# 不该插手。用 uname 判断：CCR 容器跑 Linux，本地开发机是 Darwin(macOS)。
#
# ⚠️ 这个身份是写死的 —— 拿去别的（尤其是工作/公司）仓库用之前，先确认这就是你
# 想要的身份，别把正经身份覆盖掉。
set -uo pipefail

if [ "$(uname -s)" = "Darwin" ]; then
  echo "[restore_git_identity] 本地 macOS 环境，跳过（这个 hook 只在云端容器里生效）"
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

git config --local user.name "Koharu-Mizuki"
git config --local user.email "w_mutsumi@hotmail.com"

if [ -z "${GPG_SIGNING_KEY:-}" ]; then
  echo "[restore_git_identity] 身份已配好；没有 GPG_SIGNING_KEY，跳过签名设置"
  exit 0
fi

export GNUPGHOME="${GNUPGHOME:-$HOME/.gnupg}"
mkdir -p "$GNUPGHOME" && chmod 700 "$GNUPGHOME"

echo "$GPG_SIGNING_KEY" | base64 -d | gpg --batch --import 2>/dev/null

KEY_ID=$(gpg --list-secret-keys --with-colons 2>/dev/null | awk -F: '/^sec/ {print $5; exit}')
if [ -z "$KEY_ID" ]; then
  echo "[restore_git_identity] 身份已配好；GPG_SIGNING_KEY 导入失败，跳过签名设置" >&2
  exit 0
fi

git config --local gpg.format openpgp
git config --local user.signingkey "$KEY_ID"
git config --local commit.gpgsign true

echo "[restore_git_identity] 身份 + 签名都配好了（key: $KEY_ID）"
