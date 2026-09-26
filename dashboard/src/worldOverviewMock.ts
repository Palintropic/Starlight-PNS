// Sample data for the World overview prototype. The backend has no read endpoint for world
// history or resident state yet; this file stands in for one. Shapes mirror the backend enums
// (pns/models/event.py EventType, pns/models/world_state.py ActivityKind / Availability); location,
// channel and unit ids come from pns/world/locations.py, pns/world/channels.py and packs/pjsk/units,
// so the page can later be pointed at a real endpoint without changing its vocabulary.

export type ActivityKind =
  | 'unspecified'
  | 'idle'
  | 'resting'
  | 'studying'
  | 'working_part_time'
  | 'drawing'
  | 'composing'
  | 'editing_video'
  | 'online_chatting';

export type Availability = 'available' | 'busy' | 'asleep';

export type WorldEventType =
  | 'dialogue.spoken'
  | 'message.sent'
  | 'presence.joined_channel'
  | 'presence.left_channel'
  | 'world.time_advanced'
  | 'character.location_changed'
  | 'character.activity_changed';

export interface OverviewUnit {
  id: string;
  name: string;
  short: string;
}

export interface OverviewResident {
  id: string;
  name: string;
  /** Given name, used in running text. */
  short: string;
  unit: string;
  location_id: string;
  activity: { kind: ActivityKind; since: string };
  availability: Availability;
  /** Remote presence endpoints. Not a second physical location. */
  channels: string[];
}

export interface OverviewLocation {
  id: string;
  name: string;
  parent_id: string | null;
}

export interface OverviewChannel {
  id: string;
  name: string;
}

export interface OverviewEvent {
  seq: number;
  type: WorldEventType;
  /** Simulation clock, local world time, no offset. */
  at: string;
  actor: string;
  payload: Record<string, string>;
}

export interface WorldOverview {
  world_id: string;
  clock: string;
  revision: number;
  driving: boolean;
  units: OverviewUnit[];
  residents: OverviewResident[];
  locations: OverviewLocation[];
  channels: OverviewChannel[];
  events: OverviewEvent[];
}

const units: OverviewUnit[] = [
  { id: 'leoneed', name: 'Leo/need', short: 'Leo/need' },
  { id: 'mmj', name: 'MORE MORE JUMP!', short: 'MMJ' },
  { id: 'vbs', name: 'Vivid BAD SQUAD', short: 'VBS' },
  { id: 'wxs', name: 'Wonderlands×Showtime', short: 'WxS' },
  { id: '25ji', name: '25時、ナイトコードで。', short: '25時' },
];

const locations: OverviewLocation[] = [
  { id: 'tokyo', name: '东京', parent_id: null },
  { id: 'city_streets', name: '街道', parent_id: 'tokyo' },
  { id: 'kamiyama_high', name: '神山高校', parent_id: 'tokyo' },
  { id: 'kamiyama_high_gate', name: '神山高校校门口', parent_id: 'kamiyama_high' },
  { id: 'clothing_store', name: '服装店', parent_id: 'tokyo' },
  { id: 'clothing_store_floor', name: '服装店·整理区', parent_id: 'clothing_store' },
  { id: 'ena_home', name: '绘名家', parent_id: 'tokyo' },
  { id: 'ena_home_studio', name: '绘名家·画室', parent_id: 'ena_home' },
  { id: 'mizuki_home', name: '瑞希家', parent_id: 'tokyo' },
  { id: 'mizuki_home_room', name: '瑞希家·房间', parent_id: 'mizuki_home' },
  { id: 'private_residence', name: '各自住处', parent_id: 'tokyo' },
];

const resident = (
  id: string,
  name: string,
  short: string,
  unit: string,
  location_id: string,
  kind: ActivityKind,
  since: string,
  availability: Availability,
  channels: string[] = [],
): OverviewResident => ({ id, name, short, unit, location_id, activity: { kind, since }, availability, channels });

const residents: OverviewResident[] = [
  resident('ichika', '星乃一歌', '一歌', 'leoneed', 'private_residence', 'resting', '2026-05-12T23:40', 'asleep'),
  resident('saki', '天马咲希', '咲希', 'leoneed', 'private_residence', 'resting', '2026-05-12T23:10', 'asleep'),
  resident('honami', '望月穗波', '穗波', 'leoneed', 'private_residence', 'resting', '2026-05-12T23:00', 'asleep'),
  resident('shiho', '日野森志步', '志步', 'leoneed', 'private_residence', 'resting', '2026-05-13T00:20', 'asleep'),
  resident('minori', '花里实乃理', '实乃理', 'mmj', 'private_residence', 'resting', '2026-05-12T23:05', 'asleep'),
  resident('haruka', '桐谷遥', '遥', 'mmj', 'private_residence', 'resting', '2026-05-12T23:30', 'asleep'),
  resident('airi', '桃井爱莉', '爱莉', 'mmj', 'private_residence', 'resting', '2026-05-13T00:05', 'asleep'),
  resident('shizuku', '日野森雫', '雫', 'mmj', 'private_residence', 'resting', '2026-05-12T23:15', 'asleep'),
  resident('kohane', '小豆泽心羽', '心羽', 'vbs', 'private_residence', 'resting', '2026-05-12T23:20', 'asleep'),
  resident('an', '白石杏', '杏', 'vbs', 'private_residence', 'resting', '2026-05-13T00:10', 'asleep'),
  resident('akito', '东云彰人', '彰人', 'vbs', 'ena_home', 'resting', '2026-05-13T00:40', 'asleep'),
  resident('toya', '青柳冬弥', '冬弥', 'vbs', 'private_residence', 'resting', '2026-05-13T00:30', 'asleep'),
  resident('tsukasa', '天马司', '司', 'wxs', 'private_residence', 'resting', '2026-05-12T23:50', 'asleep'),
  resident('emu', '凤笑梦', '笑梦', 'wxs', 'private_residence', 'resting', '2026-05-12T22:30', 'asleep'),
  resident('nene', '草薙宁宁', '宁宁', 'wxs', 'private_residence', 'resting', '2026-05-13T00:15', 'asleep'),
  // Awake, but nothing recorded says what he is doing: the closed set has no honest kind for it.
  resident('rui', '神代类', '类', 'wxs', 'private_residence', 'unspecified', '2026-05-12T20:40', 'available'),
  resident('kanade', '宵崎奏', '奏', '25ji', 'private_residence', 'composing', '2026-05-12T13:00', 'busy', ['nightcord']),
  resident('mafuyu', '朝比奈真冬', '真冬', '25ji', 'private_residence', 'online_chatting', '2026-05-13T01:05', 'available', ['nightcord']),
  resident('ena', '东云绘名', '绘名', '25ji', 'ena_home_studio', 'drawing', '2026-05-12T22:05', 'busy', ['nightcord']),
  resident('mizuki', '晓山瑞希', '瑞希', '25ji', 'mizuki_home_room', 'editing_video', '2026-05-12T21:20', 'available', ['nightcord']),
];

let seq = 1000;
const move = (at: string, actor: string, from: string, to: string): OverviewEvent => ({
  seq: ++seq,
  type: 'character.location_changed',
  at,
  actor,
  payload: { from, to },
});
const act = (at: string, actor: string, from: ActivityKind, to: ActivityKind): OverviewEvent => ({
  seq: ++seq,
  type: 'character.activity_changed',
  at,
  actor,
  payload: { from, to },
});
const speak = (at: string, actor: string, location: string, text: string): OverviewEvent => ({
  seq: ++seq,
  type: 'dialogue.spoken',
  at,
  actor,
  payload: { location, text },
});
const join = (at: string, actor: string): OverviewEvent => ({
  seq: ++seq,
  type: 'presence.joined_channel',
  at,
  actor,
  payload: { channel: 'nightcord' },
});
const say = (at: string, actor: string, text: string): OverviewEvent => ({
  seq: ++seq,
  type: 'message.sent',
  at,
  actor,
  payload: { channel: 'nightcord', text },
});

const GATE = 'kamiyama_high_gate';

const events: OverviewEvent[] = [
  act('2026-05-12T13:00', 'kanade', 'resting', 'composing'),
  // After school at the Kamiyama gate: WxS runs into Mizuki.
  move('2026-05-12T15:46', 'tsukasa', 'kamiyama_high', GATE),
  move('2026-05-12T15:46', 'rui', 'kamiyama_high', GATE),
  move('2026-05-12T15:48', 'mizuki', 'kamiyama_high', GATE),
  speak('2026-05-12T15:49', 'rui', GATE, '瑞希，今天也要去打工吗？'),
  speak('2026-05-12T15:49', 'mizuki', GATE, '对呀～ 类你们今天不去排练吗？'),
  speak('2026-05-12T15:50', 'rui', GATE, '正要去。今天想试一个新的舞台机关，司君已经答应当第一个体验者了。'),
  speak('2026-05-12T15:50', 'tsukasa', GATE, '等等，我什么时候答应的！？'),
  move('2026-05-12T15:52', 'mizuki', GATE, 'city_streets'),
  move('2026-05-12T15:53', 'tsukasa', GATE, 'city_streets'),
  move('2026-05-12T15:53', 'rui', GATE, 'city_streets'),
  move('2026-05-12T16:30', 'mizuki', 'city_streets', 'clothing_store'),
  move('2026-05-12T16:34', 'mizuki', 'clothing_store', 'clothing_store_floor'),
  act('2026-05-12T16:35', 'mizuki', 'idle', 'working_part_time'),
  move('2026-05-12T17:20', 'ena', 'ena_home', 'city_streets'),
  move('2026-05-12T17:45', 'ena', 'city_streets', GATE),
  move('2026-05-12T17:48', 'ena', GATE, 'kamiyama_high'),
  act('2026-05-12T17:50', 'ena', 'idle', 'studying'),
  act('2026-05-12T19:40', 'mafuyu', 'idle', 'studying'),
  act('2026-05-12T20:00', 'minori', 'idle', 'studying'),
  act('2026-05-12T20:30', 'mizuki', 'working_part_time', 'idle'),
  move('2026-05-12T20:31', 'mizuki', 'clothing_store_floor', 'clothing_store'),
  move('2026-05-12T20:32', 'mizuki', 'clothing_store', 'city_streets'),
  move('2026-05-12T20:40', 'tsukasa', 'city_streets', 'private_residence'),
  move('2026-05-12T20:40', 'rui', 'city_streets', 'private_residence'),
  move('2026-05-12T21:05', 'mizuki', 'city_streets', 'mizuki_home'),
  move('2026-05-12T21:06', 'mizuki', 'mizuki_home', 'mizuki_home_room'),
  act('2026-05-12T21:20', 'mizuki', 'idle', 'editing_video'),
  // Night course lets out as VBS walks back from street practice: the siblings meet at the gate.
  move('2026-05-12T21:22', 'akito', 'city_streets', GATE),
  move('2026-05-12T21:22', 'toya', 'city_streets', GATE),
  act('2026-05-12T21:25', 'ena', 'studying', 'idle'),
  move('2026-05-12T21:26', 'ena', 'kamiyama_high', GATE),
  speak('2026-05-12T21:27', 'akito', GATE, '……姐？这个点你才放学？'),
  speak('2026-05-12T21:27', 'ena', GATE, '夜间部本来就是这个点啊。你呢，又练到这么晚？'),
  speak('2026-05-12T21:28', 'toya', GATE, '晚上好，绘名学姐。我们正准备回去。'),
  speak('2026-05-12T21:28', 'ena', GATE, '那正好，彰人，回家路上帮我去便利店买个布丁。'),
  speak('2026-05-12T21:28', 'akito', GATE, '哈？凭什么是我。'),
  move('2026-05-12T21:29', 'akito', GATE, 'city_streets'),
  move('2026-05-12T21:29', 'ena', GATE, 'city_streets'),
  move('2026-05-12T21:29', 'toya', GATE, 'city_streets'),
  move('2026-05-12T22:00', 'ena', 'city_streets', 'ena_home'),
  move('2026-05-12T22:00', 'akito', 'city_streets', 'ena_home'),
  move('2026-05-12T22:02', 'ena', 'ena_home', 'ena_home_studio'),
  act('2026-05-12T22:05', 'ena', 'idle', 'drawing'),
  move('2026-05-12T22:10', 'toya', 'city_streets', 'private_residence'),
  act('2026-05-12T23:05', 'minori', 'studying', 'resting'),
  act('2026-05-12T23:30', 'mafuyu', 'studying', 'idle'),
  act('2026-05-13T00:40', 'akito', 'idle', 'resting'),
  join('2026-05-13T00:58', 'kanade'),
  join('2026-05-13T01:00', 'mizuki'),
  say('2026-05-13T01:02', 'mizuki', '晚上好~ 今天打工的时候看到一件超可爱的外套，差点就把工资花掉了（笑）'),
  say('2026-05-13T01:03', 'kanade', '……晚上好。新曲的副歌我改了一版，等下发出来。'),
  join('2026-05-13T01:03', 'ena'),
  say('2026-05-13T01:04', 'ena', '等一下，我这边颜色还没定……奏，副歌的情绪是往上走的吗？'),
  join('2026-05-13T01:05', 'mafuyu'),
  act('2026-05-13T01:05', 'mafuyu', 'idle', 'online_chatting'),
  say('2026-05-13T01:06', 'mafuyu', '晚上好。作业刚写完，可以开始了。'),
  say('2026-05-13T01:07', 'kanade', '嗯，比上一版更往上。不过最后一句要收回来。'),
  say('2026-05-13T01:15', 'mizuki', '收回来的话，MV 最后那个镜头我想拉远一点～'),
  say('2026-05-13T01:31', 'ena', '……瑞希，你那件外套到底是什么颜色，我现在满脑子都是它'),
  say('2026-05-13T01:32', 'mizuki', '诶嘿，保密！'),
];

const leave = (at: string, actor: string): OverviewEvent => ({
  seq: ++seq,
  type: 'presence.left_channel',
  at,
  actor,
  payload: { channel: 'nightcord' },
});

/**
 * What the world does after the sample clock, for the "advance 5 minutes" demo. Only events up to
 * the new clock are revealed; past the end of this script the world simply stays quiet.
 */
export const MOCK_UPCOMING_EVENTS: OverviewEvent[] = [
  say('2026-05-13T01:44', 'kanade', '副歌发出来了。瑞希说的拉远镜头，和最后一句收回来应该对得上。'),
  say('2026-05-13T01:46', 'mizuki', '收到～ 我戴上耳机听一下！'),
  say('2026-05-13T01:46', 'ena', '……这版好。颜色我知道怎么调了。'),
  say('2026-05-13T01:53', 'mafuyu', '副歌第二句的词我想换一个字，明天整理好发给奏。今天先下了。'),
  leave('2026-05-13T01:54', 'mafuyu'),
  act('2026-05-13T01:54', 'mafuyu', 'online_chatting', 'resting'),
  say('2026-05-13T02:01', 'ena', '我再画一会儿，有草稿了发群里。'),
  say('2026-05-13T02:02', 'mizuki', '那我趁热把这段镜头先剪出来～'),
];

export const MOCK_WORLD_OVERVIEW: WorldOverview = {
  world_id: 'sekai',
  clock: '2026-05-13T01:42',
  revision: 214,
  driving: true,
  units,
  residents,
  locations,
  channels: [{ id: 'nightcord', name: 'Nightcord' }],
  events,
};
