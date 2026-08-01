export type Character = 'mzk' | 'ena' | string;

export interface Turn {
  session_id: string;
  turn: number;
  character: Character;
  char_name: string;
  text: string;
  drift_score: number;
  confidence: number;
  drift_type: string;
  reason: string;
  needs_human_review: boolean;
  correction: string | null;
  timestamp: string;
}

export type DecisionValue = 'approve' | 'reject' | 'rewrite';

export interface Decision {
  session_id: string;
  turn: number;
  character: string;
  decision: DecisionValue;
  note?: string | null;
  decided_at: string;
}

export type DecisionMap = Record<string, Decision>;

export const decisionKey = (sessionId: string, turn: number): string => `${sessionId}:${turn}`;
