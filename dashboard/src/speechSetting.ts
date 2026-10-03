import type { OverviewEvent } from './api';

// Where a spoken or sent line happened, read only from what the event itself recorded.
//
// Only two scopes say anything about company. In `location` scope, `participants` is who was on
// the same location node when the line was committed; in `channel` scope, who was in the channel.
// In the other scopes it lists who was addressed, which says nothing about who was present.
// None of them says who the line was addressed to or who heard it: hearing is decided later by
// exposure, which this view does not see. So the labels below only ever claim co-presence.
//
// A list is only known to be complete if the backend checked it at commit time. The overview
// reports the first event sequence number from which that check applied
// (`speech_occupancy_checked_from`); lines before it carry a list nobody verified.
export type SpeechSetting =
  | { kind: 'alone' }
  | { kind: 'together'; others: string[] }
  // `others` is null when the channel list was not checked, so who else was online is unknown;
  // the medium itself is still certain from the channel scope.
  | { kind: 'online'; others: string[] | null }
  // The record does not support any of the claims above; show the line without a setting.
  | { kind: 'unknown' };

const SPEECH_TYPES = new Set(['dialogue.spoken', 'message.sent']);

export function isSpeech(event: OverviewEvent): boolean {
  return SPEECH_TYPES.has(event.type);
}

/**
 * Classify a speech event. `notTogether` holds location ids where sharing the id does not mean
 * being together (the city, open streets, everyone's separate homes). `checkedFrom` is the
 * overview's `speech_occupancy_checked_from`.
 */
export function speechSetting(
  event: OverviewEvent,
  notTogether: ReadonlySet<string>,
  checkedFrom: number | null,
): SpeechSetting {
  const actor = event.actor;
  if (!isSpeech(event) || actor === null) return { kind: 'unknown' };
  const checked = checkedFrom !== null && event.seq >= checkedFrom && event.participants.includes(actor);
  const others = event.participants.filter((id) => id !== actor);
  if (event.scope === 'channel') {
    if (event.channel_id === null) return { kind: 'unknown' };
    return { kind: 'online', others: checked ? others : null };
  }
  if (event.scope !== 'location') return { kind: 'unknown' };
  if (event.location_id === null || event.channel_id !== null || !checked) return { kind: 'unknown' };
  if (others.length === 0) return { kind: 'alone' };
  if (notTogether.has(event.location_id)) return { kind: 'unknown' };
  return { kind: 'together', others };
}
