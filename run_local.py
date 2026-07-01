#!/usr/bin/env python3
# run_local.py — PNS 本机运行版 v0.2
# 依赖：requests（pip install requests）
# 用法（Windows）：
#   set MIMO_API_KEY=你的key && python run_local.py
# 用法（Mac/Linux）：
#   MIMO_API_KEY=xxx python run_local.py

import os, sys, json, requests
from world import (
    SCENES, DEFAULT_SCENE,
    get_ena_system, get_mzk_system, ROUTER_SYSTEM,
)

# ─── 配置 ─────────────────────────────────────────────────
API_KEY     = os.environ.get("MIMO_API_KEY", "")
BASE_URL    = "https://api.xiaomimiao.com/anthropic"
CHAR_MODEL  = "mimo-v2.5-pro"
JUDGE_MODEL = "mimo-v2.5-pro"
MAX_TOKENS  = 512
OOC_THRESHOLD = 5
DILUTION_TURN  = 7          # 超过此轮警告 context dilution

# Windows 终端颜色（ANSI，Win10+ 默认支持）
C = {
    "ena":    "\033[95m",
    "mzk":   "\033[96m",
    "router": "\033[33m",
    "warn":   "\033[91m",
    "sys":    "\033[90m",
    "bold":   "\033[1m",
    "reset":  "\033[0m",
}

# ─── API ──────────────────────────────────────────────────
def call_api(system: str, messages: list, model: str = CHAR_MODEL) -> str:
    if not API_KEY:
        print(f"{C['warn']}[ERROR] 未设置 MIMO_API_KEY{C['reset']}", file=sys.stderr)
        sys.exit(1)
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    headers = {"Content-Type": "application/json", "api-key": API_KEY}
    try:
        resp = requests.post(f"{BASE_URL}/v1/messages", headers=headers,
                             json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except requests.exceptions.Timeout:
        return "[API超时，请重试]"
    except requests.exceptions.HTTPError as e:
        return f"[API错误 {e.response.status_code}]"

# ─── Router ───────────────────────────────────────────────
def router_eval(character: str, utterance: str) -> dict:
    prompt = f"角色：{character}\n台词：{utterance}"
    raw = call_api(ROUTER_SYSTEM, [{"role": "user", "content": prompt}],
                   model=JUDGE_MODEL)
    clean = raw.strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1] if len(parts) > 1 else clean
        if clean.startswith("json"):
            clean = clean[4:].strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return {
            "character": character, "score": -1,
            "drift_type": "解析失败", "reason": raw[:80],
            "is_ooc": False, "needs_human_review": True, "correction": None,
        }

# ─── 显示 ─────────────────────────────────────────────────
LABELS = {"ena": "绘名", "mzk": "瑞希", "router": "Router"}

def cprint(role: str, text: str):
    c = C.get(role, "")
    label = LABELS.get(role, role)
    print(f"\n{c}{C['bold']}[{label}]{C['reset']}{c} {text}{C['reset']}")

def print_router(result: dict):
    score = result["score"]
    is_ooc = result["is_ooc"]
    color  = C["warn"] if is_ooc else C["router"]
    flag   = "⚠  OOC" if is_ooc else "✓"
    review = "  🔍待人工校验" if result.get("needs_human_review") else ""
    print(f"{color}  [Router] {flag}  score={score}  {result['drift_type']}  {result['reason']}{review}{C['reset']}")
    if result.get("correction"):
        print(f"{color}  → 纠正：{result['correction']}{C['reset']}")

def print_scene_banner(scene: dict):
    print(f"\n{C['sys']}{'─'*55}")
    print(f"  场景切换 → {C['bold']}{scene['label']}{C['reset']}{C['sys']}")
    print(f"  {scene['time']}  |  {scene['weather']}")
    print(f"  [{scene['lore_tag']}] {scene['trigger']}")
    print(f"{'─'*55}{C['reset']}")

def print_help():
    print(f"""
{C['sys']}指令列表：
  scene <id>   手动切换场景  id: gate / ena_room / clothes_shop / nightcord
  scenes       列出所有场景
  router off   关闭Router评分
  router on    开启Router评分
  exit         退出
  （直接回车）  让瑞希自动接绘名的话推进对话{C['reset']}""")

def list_scenes():
    print(f"\n{C['sys']}可用场景：")
    for sid, s in SCENES.items():
        auto = f"→ 自动流向 {s['auto_next']}" if s.get("auto_next") else ""
        print(f"  {sid:15s}  {s['label']}  {s['time']}  {auto}")
    print(C['reset'], end="")

# ─── 场景切换 ─────────────────────────────────────────────
class SessionState:
    def __init__(self, scene_id: str):
        self.load_scene(scene_id)
        self.ena_history: list = []
        self.mzk_history: list = []
        self.router_on   = True
        self.turn        = 0
        self.ena_sys_ext = ""   # 累积纠正注入
        self.mzk_sys_ext = ""

    def load_scene(self, scene_id: str):
        if scene_id not in SCENES:
            print(f"{C['warn']}[错误] 未知场景：{scene_id}{C['reset']}")
            return
        self.scene = SCENES[scene_id]

    def switch_scene(self, scene_id: str):
        """切换场景：更新世界状态，历史保留（但注入过渡提示）。"""
        if scene_id not in SCENES:
            print(f"{C['warn']}[错误] 未知场景：{scene_id}，输入 scenes 查看列表{C['reset']}")
            return
        self.load_scene(scene_id)
        print_scene_banner(self.scene)
        # 向两个角色的 history 各注入一条场景过渡消息
        transition = f"（场景切换：{self.scene['trigger']}）"
        self.ena_history.append({"role": "user", "content": transition})
        self.mzk_history.append({"role": "user", "content": transition})
        self.turn = 0   # 重置轮数（dilution 从新场景重新计）

    def check_auto_advance(self):
        """检查是否到达自动推进阈值。"""
        auto_turns = self.scene.get("auto_turns")
        auto_next  = self.scene.get("auto_next")
        if auto_turns and auto_next and self.turn >= auto_turns:
            print(f"\n{C['sys']}[系统] 已到达 {self.scene['label']} 自然结束点，"
                  f"时间线自动推进…{C['reset']}")
            self.switch_scene(auto_next)

    def get_ena_sys(self):
        return get_ena_system(self.scene) + self.ena_sys_ext

    def get_mzk_sys(self):
        return get_mzk_system(self.scene) + self.mzk_sys_ext

# ─── 主循环 ───────────────────────────────────────────────
def run_session(start_scene: str = DEFAULT_SCENE):
    # Windows 终端启用 ANSI
    if sys.platform == "win32":
        os.system("color")

    print(f"\n{C['bold']}{'='*55}")
    print("  Project Nightcord Sanctuary  v0.3")
    print(f"{'='*55}{C['reset']}")
    print_help()

    state = SessionState(start_scene)
    print_scene_banner(state.scene)

    # ── 初始触发 ─────────────────────────────────────────
    init_msg = f"（场景开始：{state.scene['trigger']}）"
    state.mzk_history.append({"role": "user", "content": init_msg})
    mzk_reply = call_api(state.get_mzk_sys(), state.mzk_history)
    state.mzk_history.append({"role": "assistant", "content": mzk_reply})
    cprint("mzk", mzk_reply)

    if state.router_on:
        result = router_eval("mzk", mzk_reply)
        print_router(result)
        if result.get("correction"):
            state.mzk_sys_ext += f"\n\n【本轮纠正】{result['correction']}"

    # ── 主循环 ───────────────────────────────────────────
    while True:
        state.turn += 1

        # context dilution 警告
        if state.router_on and state.turn == DILUTION_TURN:
            print(f"\n{C['warn']}[系统] 第{DILUTION_TURN}轮：context dilution 风险，"
                  f"Router 已激活完整重注入模式{C['reset']}")

        # —— 绘名回应 ——
        last_mzk = state.mzk_history[-1]["content"]
        state.ena_history.append({"role": "user", "content": last_mzk})
        ena_reply = call_api(state.get_ena_sys(), state.ena_history)
        state.ena_history.append({"role": "assistant", "content": ena_reply})
        cprint("ena", ena_reply)

        if state.router_on:
            result = router_eval("ena", ena_reply)
            print_router(result)
            if result.get("correction"):
                state.ena_sys_ext += f"\n\n【本轮纠正】{result['correction']}"

        # —— 用户输入 ——
        print()
        try:
            raw = input(f"{C['sys']}> {C['reset']}").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[退出]")
            break

        # 指令解析
        if raw.lower() == "exit":
            print("[会话结束]")
            break
        elif raw.lower() == "router off":
            state.router_on = False
            print(f"{C['sys']}[Router 已关闭]{C['reset']}")
            raw = ""
        elif raw.lower() == "router on":
            state.router_on = True
            print(f"{C['sys']}[Router 已开启]{C['reset']}")
            raw = ""
        elif raw.lower() == "scenes":
            list_scenes()
            raw = ""
        elif raw.lower().startswith("scene "):
            sid = raw.split(None, 1)[1].strip()
            state.switch_scene(sid)
            raw = ""
        elif raw.lower() in ("help", "?"):
            print_help()
            raw = ""

        # —— 自动场景推进检查 ——
        state.check_auto_advance()

        # —— 瑞希回应 ——
        mzk_input = raw if raw else ena_reply
        state.mzk_history.append({"role": "user", "content": mzk_input})
        mzk_reply = call_api(state.get_mzk_sys(), state.mzk_history)
        state.mzk_history.append({"role": "assistant", "content": mzk_reply})
        cprint("mzk", mzk_reply)

        if state.router_on:
            result = router_eval("mzk", mzk_reply)
            print_router(result)
            if result.get("correction"):
                state.mzk_sys_ext += f"\n\n【本轮纠正】{result['correction']}"


if __name__ == "__main__":
    # 支持命令行指定起始场景：python run_local.py nightcord
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCENE
    if start not in SCENES:
        print(f"未知场景 '{start}'，可选：{list(SCENES.keys())}")
        sys.exit(1)
    run_session(start)
