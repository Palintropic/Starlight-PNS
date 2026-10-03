import type { OverviewEvent } from './api';

// Where a spoken or sent line happened, read only from what the event itself recorded.
//
// `participants` on a speech event is who occupied the same location node (or was in the
// channel) when the line was committed. It is not who the line was addressed to, and not who
// heard it: hearing is decided later by exposure, which this view does not see. So the labels
// below only ever claim co-presence.
export type SpeechSetting =
  | { kind: 'alone' }
  | { kind: 'together'; others: string[] }
  // `others` is null when the channel record does not list the speaker, so who else was online
  // is unknown; the medium itself is still certain from the channel id.
  | { kind: 'online'; others: string[] | null }
  // The record does not support any of the claims above; show the line without a setting.
  | { kind: 'unknown' };

const SPEECH_TYPES = new Set(['dialogue.spoken', 'message.sent']);

export function isSpeech(event: OverviewEvent): boolean {
  return SPEECH_TYPES.has(event.type);
}

/**
 * Classify a speech event. `notTogether` holds location ids where sharing the id does not mean
 * being together (the city, open streets, everyone's separate homes).
 */
export function speechSetting(event: OverviewEvent, notTogether: ReadonlySet<string>): SpeechSetting {
  const actor = event.actor;
  if (!isSpeech(event) || actor === null) return { kind: 'unknown' };
  const others = event.participants.filter((id) => id !== actor);
  // Committed speech always lists its speaker among those present. A record without the speaker
  // was not written that way, so it cannot tell us who else was there either.
  const recorded = event.participants.includes(actor);
  if (event.channel_id !== null) return { kind: 'online', others: recorded ? others : null };
  if (event.location_id === null) return { kind: 'unknown' };
  if (!recorded) return { kind: 'unknown' };
  if (others.length === 0) return { kind: 'alone' };
  if (notTogether.has(event.location_id)) return { kind: 'unknown' };
  return { kind: 'together', others };
}
