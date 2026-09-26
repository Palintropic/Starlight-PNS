import { Fragment, useMemo, useState } from 'react';
import CharacterAvatar from './CharacterAvatar';
import {
  MOCK_UPCOMING_EVENTS,
  MOCK_WORLD_OVERVIEW,
  type ActivityKind,
  type Availability,
  type OverviewEvent,
  type OverviewResident,
  type WorldOverview as WorldOverviewData,
} from './worldOverviewMock';
import './worldOverview.css';

const ACTIVITY_LABEL: Record<ActivityKind, string> = {
  unspecified: '未记录活动',
  idle: '空闲',
  resting: '休息',
  studying: '学习',
  working_part_time: '打工',
  drawing: '画画',
  composing: '作曲',
  editing_video: '剪视频',
  online_chatting: '线上聊天',
};

const AVAILABILITY_LABEL: Record<Availability, string> = {
  available: '醒着',
  busy: '专注中',
  asleep: '睡着',
};

const WEEKDAY = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

// A gap this long between two world events is drawn as a quiet stretch. Silence is a valid
// state, so it is shown as a fact about the world, not as a warning. Threshold by eye.
const QUIET_GAP_MINUTES = 60;

// Nodes where sharing a location id does not mean being together: the city itself, open
// streets, and `private_residence`, which stands for everyone's separate homes.
const NOT_TOGETHER = new Set(['tokyo', 'city_streets', 'private_residence']);

// Simulation clock values are local world time without an offset; parse them as UTC so the
// viewer's own timezone never shifts world time.
function parseClock(value: string): Date {
  return new Date(`${value}:00Z`);
}
function timeText(value: string): string {
  return value.slice(11, 16);
}
function dateText(value: string): string {
  const d = parseClock(value);
  return `${d.getUTCMonth() + 1}月${d.getUTCDate()}日 ${WEEKDAY[d.getUTCDay()]}`;
}
function durationText(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m} 分钟`;
  return m === 0 ? `${h} 小时` : `${h} 小时 ${m} 分钟`;
}
function minutesBetween(a: string, b: string): number {
  return Math.round((parseClock(b).getTime() - parseClock(a).getTime()) / 60000);
}
function addMinutes(value: string, minutes: number): string {
  return new Date(parseClock(value).getTime() + minutes * 60000).toISOString().slice(0, 16);
}

const ADVANCE_MINUTES = 5;

// Demo only: move the clock forward and apply whatever the script says happened in that window.
// Each event updates resident state the way its type says; nothing else changes.
function advanceWorld(world: WorldOverviewData, minutes: number): { world: WorldOverviewData; added: OverviewEvent[] } {
  const clock = addMinutes(world.clock, minutes);
  const added = MOCK_UPCOMING_EVENTS.filter((e) => e.at > world.clock && e.at <= clock);
  const residents = world.residents.map((r) => {
    let next = r;
    for (const e of added) {
      if (e.actor !== r.id) continue;
      if (e.type === 'character.location_changed') next = { ...next, location_id: e.payload.to };
      if (e.type === 'character.activity_changed')
        next = { ...next, activity: { kind: e.payload.to as ActivityKind, since: e.at } };
      if (e.type === 'presence.joined_channel' && !next.channels.includes(e.payload.channel))
        next = { ...next, channels: [...next.channels, e.payload.channel] };
      if (e.type === 'presence.left_channel')
        next = { ...next, channels: next.channels.filter((c) => c !== e.payload.channel) };
    }
    return next;
  });
  const tick: OverviewEvent = {
    seq: world.events.length + added.length + 100000,
    type: 'world.time_advanced',
    at: clock,
    actor: 'world',
    payload: { from: world.clock, to: clock },
  };
  return {
    world: {
      ...world,
      clock,
      revision: world.revision + added.length + 1,
      residents,
      events: [...world.events, ...added, tick],
    },
    added,
  };
}

export default function WorldOverview() {
  const [data, setData] = useState<WorldOverviewData>(MOCK_WORLD_OVERVIEW);
  const [lastAdvance, setLastAdvance] = useState<{ from: string; to: string; added: OverviewEvent[] } | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [unitFilter, setUnitFilter] = useState<string | null>(null);

  const locationById = useMemo(() => new Map(data.locations.map((l) => [l.id, l])), [data]);
  const residentById = useMemo(() => new Map(data.residents.map((r) => [r.id, r])), [data]);
  const unitShort = (id: string) => data.units.find((u) => u.id === id)?.short ?? id;
  const channelName = (id: string) => data.channels.find((c) => c.id === id)?.name ?? id;
  const locationName = (id: string) => locationById.get(id)?.name ?? id;
  const shortName = (id: string) => residentById.get(id)?.short ?? id;
  const inFilter = (id: string) => !unitFilter || residentById.get(id)?.unit === unitFilter;

  // Physical grouping: everyone appears exactly once. Together (two or more in one place),
  // awake on their own, or asleep. Remote presence is grouped separately below.
  const groups = useMemo(() => {
    // The building a location belongs to: the ancestor directly under the root.
    const placeOf = (locationId: string): string => {
      let cur = locationById.get(locationId);
      while (cur?.parent_id && locationById.get(cur.parent_id)?.parent_id) cur = locationById.get(cur.parent_id);
      return cur?.id ?? locationId;
    };
    const byPlace = new Map<string, OverviewResident[]>();
    for (const r of data.residents) {
      if (NOT_TOGETHER.has(r.location_id)) continue;
      const place = placeOf(r.location_id);
      byPlace.set(place, [...(byPlace.get(place) ?? []), r]);
    }
    const together = [...byPlace.entries()].filter(([, rs]) => rs.length >= 2);
    const grouped = new Set(together.flatMap(([, rs]) => rs.map((r) => r.id)));
    const rest = data.residents.filter((r) => !grouped.has(r.id));
    return {
      together,
      awake: rest.filter((r) => r.availability !== 'asleep'),
      asleep: rest.filter((r) => r.availability === 'asleep'),
    };
  }, [data, locationById]);

  const visibleEvents = useMemo(
    () =>
      data.events.filter(
        (e) =>
          e.type !== 'world.time_advanced' &&
          (focus ? e.actor === focus : !unitFilter || residentById.get(e.actor)?.unit === unitFilter),
      ),
    [data, focus, unitFilter, residentById],
  );

  // Newest first, grouped by world date, with quiet stretches between events.
  const timeline = useMemo(() => {
    const items: (
      | { kind: 'date'; key: string; label: string }
      | { kind: 'event'; key: string; event: OverviewEvent }
      | { kind: 'quiet'; key: string; minutes: number }
    )[] = [];
    const desc = [...visibleEvents].reverse();
    const nowGap = desc.length ? minutesBetween(desc[0].at, data.clock) : 0;
    let lastDate = '';
    if (nowGap >= QUIET_GAP_MINUTES) items.push({ kind: 'quiet', key: 'q-now', minutes: nowGap });
    desc.forEach((event, i) => {
      const day = event.at.slice(0, 10);
      if (day !== lastDate) {
        items.push({ kind: 'date', key: `d-${day}`, label: dateText(event.at) });
        lastDate = day;
      }
      items.push({ kind: 'event', key: `e-${event.seq}`, event });
      const older = desc[i + 1];
      if (older) {
        const gap = minutesBetween(older.at, event.at);
        if (gap >= QUIET_GAP_MINUTES) items.push({ kind: 'quiet', key: `q-${event.seq}`, minutes: gap });
      }
    });
    return items;
  }, [visibleEvents, data.clock]);

  const onAdvance = () => {
    const { world, added } = advanceWorld(data, ADVANCE_MINUTES);
    setLastAdvance({ from: data.clock, to: world.clock, added });
    setData(world);
  };
  const onReset = () => {
    setData(MOCK_WORLD_OVERVIEW);
    setLastAdvance(null);
  };
  const newSeqs = new Set(lastAdvance?.added.map((e) => e.seq) ?? []);

  const toggleFocus = (id: string) => setFocus(focus === id ? null : id);

  const residentRow = (r: OverviewResident, detail: string) => (
    <li key={r.id}>
      <button
        className={`wo-resident${focus === r.id ? ' active' : ''}`}
        onClick={() => toggleFocus(r.id)}
        aria-pressed={focus === r.id}
      >
        <CharacterAvatar character={r.id} name={r.name} />
        <span className="wo-resident-text">
          <span className="wo-resident-name">
            {r.name}
            <span className="wo-unit">{unitShort(r.unit)}</span>
          </span>
          <span className="wo-resident-meta">{detail}</span>
        </span>
      </button>
    </li>
  );
  const activityText = (r: OverviewResident) =>
    `${ACTIVITY_LABEL[r.activity.kind]}（${timeText(r.activity.since)} 起）`;

  const renderEvent = (e: OverviewEvent) => {
    const who = <strong>{shortName(e.actor)}</strong>;
    switch (e.type) {
      case 'character.location_changed':
        return <>{who} 从 {locationName(e.payload.from)} 到了 {locationName(e.payload.to)}</>;
      case 'character.activity_changed':
        return <>{who} 从{ACTIVITY_LABEL[e.payload.from as ActivityKind]}转为{ACTIVITY_LABEL[e.payload.to as ActivityKind]}</>;
      case 'presence.joined_channel':
        return <>{who} 进入 {channelName(e.payload.channel)}</>;
      case 'presence.left_channel':
        return <>{who} 离开 {channelName(e.payload.channel)}</>;
      case 'message.sent':
        return (
          <>
            {who} 在 {channelName(e.payload.channel)}
            <span className="wo-quote">{e.payload.text}</span>
          </>
        );
      case 'dialogue.spoken':
        return (
          <>
            {who} 在 {locationName(e.payload.location)} 说
            <span className="wo-quote">{e.payload.text}</span>
          </>
        );
      default:
        return <>{who} · {e.type}</>;
    }
  };

  const together = groups.together
    .map(([place, rs]) => [place, rs.filter((r) => inFilter(r.id))] as const)
    .filter(([, rs]) => rs.length);
  const awake = groups.awake.filter((r) => inFilter(r.id));
  const asleep = groups.asleep.filter((r) => inFilter(r.id));
  const online = data.channels
    .map((c) => [c, data.residents.filter((r) => r.channels.includes(c.id) && inFilter(r.id))] as const)
    .filter(([, rs]) => rs.length);

  return (
    <div className="world-overview">
      <div className="wo-head">
        <div className="wo-clock">
          <span className="wo-time">{timeText(data.clock)}</span>
          <span className="wo-date">{dateText(data.clock)} · 世界时间</span>
        </div>
        <div className="wo-state">
          <span className={`status-dot${data.driving ? ' running' : ''}`} />
          <span>{data.driving ? '自动推进中' : '已暂停'}</span>
          <span className="wo-mono">{data.world_id} · rev {data.revision}</span>
          <button className="btn btn-accent" onClick={onAdvance}>
            推进 {ADVANCE_MINUTES} 分钟
          </button>
          {lastAdvance ? (
            <button className="btn" onClick={onReset}>
              重置
            </button>
          ) : null}
        </div>
      </div>
      {lastAdvance ? (
        <p className="wo-advance" role="status">
          {timeText(lastAdvance.from)} → {timeText(lastAdvance.to)}：
          {lastAdvance.added.length
            ? `${lastAdvance.added.length} 条新的世界事件`
            : '这 5 分钟里世界没有发生需要记录的事，时间照样走。'}
        </p>
      ) : null}
      <p className="wo-sample-note">示例数据：后端还没有世界历史和居民状态的只读接口，这一页用的是前端内置的假数据；「推进」只在这个页面里播放预先写好的剧本，不会调用模型，也不会写入任何世界。</p>

      <div className="wo-body">
        <div className="wo-side">
          <div className="wo-filter" role="group" aria-label="按团筛选">
            <button className={`toggle${unitFilter === null ? ' on' : ''}`} onClick={() => setUnitFilter(null)}>
              全部
            </button>
            {data.units.map((u) => (
              <button
                key={u.id}
                className={`toggle${unitFilter === u.id ? ' on' : ''}`}
                onClick={() => setUnitFilter(unitFilter === u.id ? null : u.id)}
                title={u.name}
              >
                {u.short}
              </button>
            ))}
          </div>

          {online.length ? (
            <section>
              <h3 className="wo-section">线上</h3>
              {online.map(([c, rs]) => (
                <div key={c.id} className="wo-group">
                  <h4 className="wo-subsection">{c.name} · {rs.length} 人</h4>
                  <ul className="wo-residents">
                    {rs.map((r) => residentRow(r, `人在${locationName(r.location_id)} · ${activityText(r)}`))}
                  </ul>
                </div>
              ))}
              <p className="wo-hint">线上在场是远程接入，不改变物理位置，所以同一个人也会出现在下面的线下分组里。</p>
            </section>
          ) : null}

          <section>
            <h3 className="wo-section">线下</h3>
            {together.map(([place, rs]) => (
              <div key={place} className="wo-group">
                <h4 className="wo-subsection">同在{locationName(place)} · {rs.length} 人</h4>
                <ul className="wo-residents">
                  {rs.map((r) =>
                    residentRow(r, `${locationName(r.location_id)} · ${AVAILABILITY_LABEL[r.availability]} · ${activityText(r)}`),
                  )}
                </ul>
              </div>
            ))}
            {awake.length ? (
              <div className="wo-group">
                <h4 className="wo-subsection">各自醒着 · {awake.length} 人</h4>
                <ul className="wo-residents">
                  {awake.map((r) => residentRow(r, `${locationName(r.location_id)} · ${activityText(r)}`))}
                </ul>
              </div>
            ) : null}
            {asleep.length ? (
              <div className="wo-group">
                <h4 className="wo-subsection">睡着 · {asleep.length} 人</h4>
                <ul className="wo-sleepers">
                  {asleep.map((r) => (
                    <li key={r.id}>
                      <button
                        className={`wo-sleeper${focus === r.id ? ' active' : ''}`}
                        onClick={() => toggleFocus(r.id)}
                        aria-pressed={focus === r.id}
                        title={`${r.name} · ${unitShort(r.unit)} · ${locationName(r.location_id)} · ${timeText(r.activity.since)} 起`}
                      >
                        <CharacterAvatar character={r.id} name={r.name} />
                        <span>{r.short}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </section>
        </div>

        <section className="wo-main">
          <div className="wo-main-head">
            <h3 className="wo-section">
              {focus
                ? `${shortName(focus)}发起的世界事件`
                : unitFilter
                  ? `世界历史 · ${unitShort(unitFilter)}`
                  : '世界历史'}
            </h3>
            {focus ? (
              <button className="command" onClick={() => setFocus(null)}>
                显示全部
              </button>
            ) : null}
          </div>
          {focus ? (
            <p className="wo-hint">
              这是世界记录里由{shortName(focus)}发起的事件，不等于{shortName(focus)}经历或记得的内容。
            </p>
          ) : null}
          <ol className="wo-timeline">
            {timeline.map((item) => (
              <Fragment key={item.key}>
                {item.kind === 'date' ? (
                  <li className="wo-date-row">{item.label}</li>
                ) : item.kind === 'quiet' ? (
                  <li className="wo-quiet">{durationText(item.minutes)}没有世界事件</li>
                ) : (
                  <li className={`wo-event${newSeqs.has(item.event.seq) ? ' new' : ''}`}>
                    <span className="wo-event-time">{timeText(item.event.at)}</span>
                    <span className="wo-event-text">{renderEvent(item.event)}</span>
                  </li>
                )}
              </Fragment>
            ))}
          </ol>
        </section>
      </div>
    </div>
  );
}
