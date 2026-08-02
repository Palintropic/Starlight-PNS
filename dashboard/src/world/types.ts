export type DayPhase = 'morning' | 'afternoon' | 'evening' | 'late_night';
export type LoreTag = '硬事实' | '软推断' | '待验证';

export interface Scene {
  id: string;
  label: string;
  time: string;
  location: string;
  weather: string;
  day_phase: DayPhase;
  scene_type: string;
  lore_tag: LoreTag;
  trigger: string;
  gate_triggers?: Record<string, string> | null;
  gate_opening_note?: string | null;
  auto_next?: string | null;
  auto_turns?: number | null;
}

export type ScenesMap = Record<string, Scene>;

export interface FactsResponse {
  facts: Record<string, string>;
  groups: Record<string, string[]>;
}

export const blankScene = (id: string): Scene => ({
  id,
  label: '',
  time: '',
  location: '',
  weather: '',
  day_phase: 'morning',
  scene_type: 'area_talk',
  lore_tag: '待验证',
  trigger: '',
  gate_triggers: null,
  gate_opening_note: null,
  auto_next: null,
  auto_turns: null,
});
