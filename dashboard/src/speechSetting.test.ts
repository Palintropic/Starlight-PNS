// dashboard/src/speechSetting.test.ts — reading a speech event's setting from its own record.
//
// The timeline labels each line as alone / together / online. Every label is a claim about
// the world, so each case here pins one record shape to the only claim it supports. Two inputs
// beyond the record decide whether a list can be trusted at all: its scope (only `location` and
// `channel` scopes are presence lists) and whether the backend checked it at commit time.
import { describe, expect, it } from 'vitest';
import type { OverviewEvent } from './api';
import { speechSetting } from './speechSetting';

const NOT_TOGETHER = new Set(['tokyo', 'city_streets', 'private_residence']);
const CHECKED = 0; // every event in these fixtures was committed under the presence check

const speech = (overrides: Partial<OverviewEvent>): OverviewEvent => ({
  seq: 5,
  event_id: 'e1',
  type: 'dialogue.spoken',
  at: '2026-10-03T21:40:00',
  scope: 'location',
  actor: 'mizuki',
  participants: ['mizuki'],
  location_id: 'mizuki_home_room',
  channel_id: null,
  payload: { text: '……' },
  ...overrides,
});

const online = (overrides: Partial<OverviewEvent>) =>
  speech({
    type: 'message.sent',
    scope: 'channel',
    actor: 'ena',
    participants: ['ena', 'mizuki', 'kanade'],
    channel_id: 'nightcord',
    location_id: null,
    ...overrides,
  });

describe('speechSetting on checked records', () => {
  it('is alone when the speaker was the only occupant', () => {
    expect(speechSetting(speech({}), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'alone' });
  });

  it('is together when others occupied the same node, listing only the others', () => {
    const e = speech({ actor: 'kanade', participants: ['kanade', 'mafuyu'], location_id: 'kanade_home' });
    expect(speechSetting(e, NOT_TOGETHER, CHECKED)).toEqual({ kind: 'together', others: ['mafuyu'] });
  });

  it('is online for a channel line, whether spoken or sent', () => {
    for (const type of ['dialogue.spoken', 'message.sent']) {
      // The turn path records the speaker's room too; channel scope still means online.
      const e = online({ type, location_id: 'ena_home_studio' });
      expect(speechSetting(e, NOT_TOGETHER, CHECKED)).toEqual({ kind: 'online', others: ['mizuki', 'kanade'] });
    }
  });

  it('is online with no others when the speaker was alone in the channel', () => {
    expect(speechSetting(online({ participants: ['ena'] }), NOT_TOGETHER, CHECKED)).toEqual({
      kind: 'online',
      others: [],
    });
  });

  it('does not call sharing a not-together node being together', () => {
    const e = speech({ participants: ['mizuki', 'ena'], location_id: 'private_residence' });
    expect(speechSetting(e, NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
  });

  it('still reports alone on a not-together node when nobody else was there', () => {
    expect(speechSetting(speech({ location_id: 'city_streets' }), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'alone' });
  });
});

describe('speechSetting refuses what the record cannot support', () => {
  it('does not read a named-participant list as presence (review F1)', () => {
    // Named a and b; b was somewhere else. The page used to say "together with b".
    for (const scope of ['participant', 'private']) {
      const e = speech({ scope, participants: ['mizuki', 'ena'] });
      expect(speechSetting(e, NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
      const named = online({ scope });
      expect(speechSetting(named, NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
    }
  });

  it('does not trust an unchecked list to be complete (review F2)', () => {
    // Two people in the room, the record lists only the speaker, and it predates the check.
    const before = speech({ seq: 3 });
    expect(speechSetting(before, NOT_TOGETHER, 4)).toEqual({ kind: 'unknown' });
    expect(speechSetting(before, NOT_TOGETHER, null)).toEqual({ kind: 'unknown' });
    // A channel line keeps its medium but makes no claim about who else was online.
    expect(speechSetting(online({ seq: 3, participants: ['ena'] }), NOT_TOGETHER, 4)).toEqual({
      kind: 'online',
      others: null,
    });
  });

  it('starts trusting exactly at the checked-from sequence number', () => {
    expect(speechSetting(speech({ seq: 4 }), NOT_TOGETHER, 4)).toEqual({ kind: 'alone' });
  });

  it('claims nothing when anchors contradict the scope', () => {
    expect(speechSetting(speech({ channel_id: 'nightcord' }), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
    expect(speechSetting(speech({ location_id: null }), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
    expect(speechSetting(online({ channel_id: null }), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
  });

  it('claims nothing when the record does not list the speaker', () => {
    expect(speechSetting(speech({ participants: ['ena'] }), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
    expect(speechSetting(online({ participants: ['mizuki'] }), NOT_TOGETHER, CHECKED)).toEqual({
      kind: 'online',
      others: null,
    });
  });

  it('claims nothing, and does not throw, when the participant list is missing (re-review P2)', () => {
    for (const participants of [undefined, null]) {
      const raw = { participants } as unknown as Partial<OverviewEvent>;
      expect(speechSetting(speech(raw), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
      expect(speechSetting(online(raw), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'online', others: null });
    }
  });

  it('treats a missing anchor like a null one, never as alone or online (re-review P2)', () => {
    const missing = (key: keyof OverviewEvent) => {
      const raw = speech({});
      delete (raw as Partial<OverviewEvent>)[key];
      return raw;
    };
    expect(speechSetting(missing('location_id'), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
    expect(speechSetting(missing('channel_id'), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
    expect(speechSetting(missing('actor'), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
    const channel = online({});
    delete (channel as Partial<OverviewEvent>).channel_id;
    expect(speechSetting(channel, NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
  });

  it('claims nothing for a speech record without a speaker, or for non-speech events', () => {
    expect(speechSetting(speech({ actor: null }), NOT_TOGETHER, CHECKED)).toEqual({ kind: 'unknown' });
    expect(speechSetting(speech({ type: 'character.location_changed' }), NOT_TOGETHER, CHECKED)).toEqual({
      kind: 'unknown',
    });
  });
});
