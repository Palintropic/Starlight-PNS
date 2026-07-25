# pns/world/characters/registry.py
from typing import Dict, List, Optional

# 角色注册表 - 定义所有20个角色的元数据
CHARACTER_REGISTRY = {
    # 25ji - 完整样本
    'ena': {
        'name': '东云绘名',
        'name_jp': '東雲絵名',
        'status': 'ready',           # ready / partial / not_ready
        'sample_coverage': 1.0,      # 0-1
        'unit': '25ji',
        'role': '插画负责人',
        'school': '神山高校',
        'grade': 3,
        'class': '3-D',
    },
    'mzk': {
        'name': '暁山瑞希',
        'name_jp': '暁山瑞希',
        'status': 'ready',
        'sample_coverage': 1.0,
        'unit': '25ji',
        'role': '动画负责人',
        'school': '神山高校',
        'grade': 2,
        'class': '2-B',
    },
    'kanade': {
        'name': '宵崎奏',
        'name_jp': '宵崎奏',
        'status': 'partial',
        'sample_coverage': 0.3,
        'unit': '25ji',
        'role': '作曲人',
        'school': '居家自学',
        'grade': None,
        'class': None,
    },
    'mafuyu': {
        'name': '朝比奈真冬',
        'name_jp': '朝比奈真冬',
        'status': 'partial',
        'sample_coverage': 0.3,
        'unit': '25ji',
        'role': '作词人',
        'school': '宮益坂女子学院',
        'grade': 2,
        'class': '2-?',
    },
    
    # Vivid BAD SQUAD
    'akaito': {
        'name': '东云彰人',
        'name_jp': '東雲彰人',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'VBS',
        'role': '成员',
        'school': '神山高校',
        'grade': 2,
        'class': '2-A',
    },
    'oshiro_anne': {
        'name': '白石杏',
        'name_jp': '白石杏',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'VBS',
        'role': '成员',
        'school': '神山高校',
        'grade': 2,
        'class': '2-A',
    },
    'aoyagi_toya': {
        'name': '青柳冬弥',
        'name_jp': '青柳冬弥',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'VBS',
        'role': '成员',
        'school': '神山高校',
        'grade': 2,
        'class': '2-B',
    },
    'azusawa_kokoro': {
        'name': '小豆泽心羽',
        'name_jp': '小豆泽心羽',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'VBS',
        'role': '成员',
        'school': '神山高校',
        'grade': 2,
        'class': '2-A',
    },
    
    # Wonderlands×Showtime
    'amia': {
        'name': '天马司',
        'name_jp': '天馬司',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'WxS',
        'role': '成员',
        'school': '神山高校',
        'grade': 3,
        'class': '3-C',
    },
    'otori_emu': {
        'name': '凤笑梦',
        'name_jp': '鳳笑梦',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'WxS',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 2,
        'class': '2-B',
    },
    'kusanagi_nene': {
        'name': '草薙宁宁',
        'name_jp': '草薙寧々',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'WxS',
        'role': '成员',
        'school': '神山高校',
        'grade': 2,
        'class': '2-A',
    },
    'jinkoji_rui': {
        'name': '神代类',
        'name_jp': '神代類',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'WxS',
        'role': '成员',
        'school': '神山高校',
        'grade': 3,
        'class': '3-C',
    },
    
    # MORE MORE JUMP!
    'hanawa_shinori': {
        'name': '花里实乃理',
        'name_jp': '花里実乃理',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'MMJ',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 2,
        'class': '2-D',
    },
    'kiriya_haruka': {
        'name': '桐谷遥',
        'name_jp': '桐谷遥',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'MMJ',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 2,
        'class': '2-D',
    },
    'momoi_airi': {
        'name': '桃井爱莉',
        'name_jp': '桃井愛莉',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'MMJ',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 3,
        'class': '3-E',
    },
    'hinomori_shio': {
        'name': '日野森雫',
        'name_jp': '日野森雫',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'MMJ',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 3,
        'class': '3-E',
    },
    
    # Leo/need
    'hoshino_ichika': {
        'name': '星乃一歌',
        'name_jp': '星乃一歌',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'Leo/need',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 2,
        'class': '2-A',
    },
    'tenma_saki': {
        'name': '天马咲希',
        'name_jp': '天馬咲希',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'Leo/need',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 2,
        'class': '2-B',
    },
    'mochizuki_honami': {
        'name': '望月穗波',
        'name_jp': '望月穗波',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'Leo/need',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 2,
        'class': '2-A',
    },
    'hinomori_shiho': {
        'name': '日野森志步',
        'name_jp': '日野森志步',
        'status': 'not_ready',
        'sample_coverage': 0.0,
        'unit': 'Leo/need',
        'role': '成员',
        'school': '宮益坂女子学院',
        'grade': 2,
        'class': '2-B',
    },
}

def get_character_prompt(character_id: str) -> str:
    """获取角色的SYSTEM prompt"""
    # 动态导入以避免循环依赖
    from . import ena, mzk, kanade, mafuyu
    
    prompts = {
        'ena': ena.SYSTEM_PROMPT,
        'mzk': mzk.SYSTEM_PROMPT,
        'kanade': kanade.SYSTEM_PROMPT if hasattr(kanade, 'SYSTEM_PROMPT') else '',
        'mafuyu': mafuyu.SYSTEM_PROMPT if hasattr(mafuyu, 'SYSTEM_PROMPT') else '',
    }
    
    if character_id not in prompts:
        raise ValueError(f"Character not found: {character_id}")
    
    return prompts[character_id]

def get_character_metadata(character_id: str) -> Dict:
    """获取角色的元数据"""
    if character_id not in CHARACTER_REGISTRY:
        raise ValueError(f"Character not found: {character_id}")
    return CHARACTER_REGISTRY[character_id].copy()

def list_characters(include_partial: bool = False, include_not_ready: bool = False) -> Dict[str, Dict]:
    """列出所有可用角色"""
    result = {}
    for char_id, info in CHARACTER_REGISTRY.items():
        if info['status'] == 'ready':
            result[char_id] = info
        elif include_partial and info['status'] == 'partial':
            result[char_id] = info
        elif include_not_ready and info['status'] == 'not_ready':
            result[char_id] = info
    return result

def get_available_pairs() -> List[tuple]:
    """获取所有ready状态的角色组合"""
    ready_chars = list_characters(include_partial=False)
    pairs = []
    ids = list(ready_chars.keys())
    for i in range(len(ids)):
        for j in range(i+1, len(ids)):
            pairs.append((ids[i], ids[j]))
    return pairs
