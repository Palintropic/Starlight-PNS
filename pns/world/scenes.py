# pns/world/scenes.py
# 从 world.py v0.6 迁移

# 世界观确定性分级（Router判断时的重要上下文）
LORE_TIER_CANON = "CANON"
LORE_TIER_INFERRED = "INFERRED"
LORE_TIER_UNVERIFIED = "UNVERIFIED"

LORE_TIER_LABELS = {
    LORE_TIER_CANON: "硬事实",
    LORE_TIER_INFERRED: "软推断",
    LORE_TIER_UNVERIFIED: "待验证",
}

SCENES = {
    "gate": {
        "id": "gate",
        "label": "神山高校校门口",
        "time": "傍晚 17:30",
        "location": "神山高校校门口",
        "weather": "晴，微风",
        "day_phase": "evening",
        "scene_type": "area_talk",
        "lore_tag": LORE_TIER_INFERRED,
        "trigger": "瑞希放学往外走，绘名刚到校门口准备进去——两人正面碰上。",
        "gate_triggers": {
            "A": "随机碰面，无特定触发事件，瑞希随口找话题切入",
            "B": "25ji活动中发生的某个具体事件作为切入点（事件待补充）",
            "C": "瑞希故意在校门口等绘名"
        },
        "gate_opening_note": "当前脚本opening台词标记为trigger_B待修：调侃前提错误（'这个时间来学校'不成立），需补充具体25ji事件素材后重写",
        "auto_next": "nightcord",
        "auto_turns": 8,
    },
    "ena_room": {
        "id": "ena_room",
        "label": "绘名家·画室",
        "time": "深夜 01:30",
        "location": "绘名家，她的画室，台灯开着",
        "weather": "室内，窗外有雨声",
        "day_phase": "late_night",
        "scene_type": "area_talk",
        "lore_tag": LORE_TIER_CANON,
        "trigger": "绘名盯着画板发呆，手机屏幕忽然亮了。",
        "auto_next": None,
        "auto_turns": None,
    },
    "clothes_shop": {
        "id": "clothes_shop",
        "label": "瑞希打工的服装店",
        "time": "下午 15:00",
        "location": "服装店，整理区，挂满新季衣架",
        "weather": "室内，店外阳光很强",
        "day_phase": "afternoon",
        "scene_type": "area_talk",
        "lore_tag": LORE_TIER_CANON,
        "trigger": "店里客人不多，瑞希正在整理挂架，手机震了一下。",
        "auto_next": "gate",
        "auto_turns": 6,
    },
    "nightcord": {
        "id": "nightcord",
        "label": "Nightcord 线上频道",
        "time": "深夜 02:00",
        "location": "各自房间·Nightcord 语音频道",
        "weather": "室内",
        "day_phase": "late_night",
        "scene_type": "area_talk",
        "lore_tag": LORE_TIER_CANON,
        "trigger": "Nightcord频道里只有两个人在线，语音刚接通，有点安静。",
        "auto_next": None,
        "auto_turns": None,
    },
}

DEFAULT_SCENE = "gate"
