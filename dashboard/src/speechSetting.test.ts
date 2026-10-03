// dashboard/src/speechSetting.test.ts — reading a speech event's setting from its own record.
//
// The timeline labels each line as alone / together / online. Every label is a claim about
// the world, so each case here pins one record shape to the only claim it supports.
import { describe, expect, it } from 'vitest';
import type { OverviewEvent } from './api';
import { speechSetting } from './speechSetting';

const NOT_TOGETHER = new Set(['tokyo', 'city_streets', 'private_residence']);

const speech = (overrides: Partial<OverviewEvent>): OverviewEvent => ({
  seq: 1,
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

describe('speechSetting', () => {
  it('is alone when the speaker was the only occupant', () => {
    expect(speechSetting(speech({}), NOT_TOGETHER)).toEqual({ kind: 'alone' });
  });

  it('is together when others occupied the same node, listing only the others', () => {
    const e = speech({ actor: 'kanade', participants: ['kanade', 'mafuyu'], location_id: 'kanade_home' });
    expect(speechSetting(e, NOT_TOGETHER)).toEqual({ kind: 'together', others: ['mafuyu'] });
  });

  it('is online for a channel line, whether spoken or sent', () => {
    for (const type of ['dialogue.spoken', 'message.sent']) {
      const e = speech({
        type,
        scope: 'channel',
        actor: 'ena',
        participants: ['ena', 'mizuki', 'kanade'],
        channel_id: 'nightcord',
        location_id: 'ena_home_studio',
      });
      expect(speechSetting(e, NOT_TOGETHER)).toEqual({ kind: 'online', others: ['mizuki', 'kanade'] });
    }
  });

  it('is online with no others when the speaker was alone in the channel', () => {
    const e = speech({ type: 'message.sent', channel_id: 'nightcord', participants: ['mizuki'] });
    expect(speechSetting(e, NOT_TOGETHER)).toEqual({ kind: 'online', others: [] });
  });

  it('keeps the medium but claims no company when a channel record does not list the speaker', () => {
    // Otherwise an empty list would read as "alone in the channel".
    const e = speech({ type: 'message.sent', channel_id: 'nightcord', participants: [] });
    expect(speechSetting(e, NOT_TOGETHER)).toEqual({ kind: 'online', others: null });
  });

  it('claims nothing when the record does not list the speaker among the occupants', () => {
    // An empty list would otherwise read as "alone", which the record never said.
    expect(speechSetting(speech({ participants: [] }), NOT_TOGETHER)).toEqual({ kind: 'unknown' });
    expect(speechSetting(speech({ participants: ['ena'] }), NOT_TOGETHER)).toEqual({ kind: 'unknown' });
  });

  it('does not call sharing a not-together node being together', () => {
    const e = speech({ participants: ['mizuki', 'ena'], location_id: 'private_residence' });
    expect(speechSetting(e, NOT_TOGETHER)).toEqual({ kind: 'unknown' });
  });

  it('still reports alone on a not-together node when nobody else was there', () => {
    const e = speech({ location_id: 'city_streets' });
    expect(speechSetting(e, NOT_TOGETHER)).toEqual({ kind: 'alone' });
  });

  it('claims nothing for a speech record without a speaker or a place', () => {
    expect(speechSetting(speech({ actor: null }), NOT_TOGETHER)).toEqual({ kind: 'unknown' });
    expect(speechSetting(speech({ location_id: null }), NOT_TOGETHER)).toEqual({ kind: 'unknown' });
  });

  it('claims nothing for events that are not speech', () => {
    const e = speech({ type: 'character.location_changed' });
    expect(speechSetting(e, NOT_TOGETHER)).toEqual({ kind: 'unknown' });
  });
});
