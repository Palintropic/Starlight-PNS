// 字段 → 控件类型的映射，仿 MCSManager instanceConfigMap.ts 的思路：
// 加一个字段只需要在这里加一条，SceneEditor 不需要跟着改渲染逻辑。
import type { Scene } from './types';

export type FieldType = 'readonly' | 'text' | 'textarea' | 'select';

export interface FieldSchema {
  key: keyof Scene;
  label: string;
  type: FieldType;
  options?: readonly string[];
}

export const DAY_PHASE_OPTIONS = ['morning', 'afternoon', 'evening', 'late_night'] as const;
export const LORE_TAG_OPTIONS = ['硬事实', '软推断', '待验证'] as const;

// gate_triggers / gate_opening_note / auto_next / auto_turns 有专门的子控件（A/B/C 子表单、
// 场景下拉、数字输入带清空），不走这份通用 schema，在 SceneEditor 里单独渲染。
export const SCENE_FIELDS: FieldSchema[] = [
  { key: 'id', label: 'ID', type: 'readonly' },
  { key: 'label', label: '名称', type: 'text' },
  { key: 'time', label: '时间', type: 'text' },
  { key: 'location', label: '地点', type: 'text' },
  { key: 'weather', label: '天气/环境', type: 'text' },
  { key: 'day_phase', label: '时段', type: 'select', options: DAY_PHASE_OPTIONS },
  { key: 'scene_type', label: '场景类型', type: 'text' },
  { key: 'lore_tag', label: 'Lore 标签', type: 'select', options: LORE_TAG_OPTIONS },
  { key: 'trigger', label: '触发文本', type: 'textarea' },
];
