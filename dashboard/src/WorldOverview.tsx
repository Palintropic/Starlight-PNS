import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import CharacterAvatar from './CharacterAvatar';
import {
  ApiError,
  fetchPersistentWorlds,
  fetchWorldOverview,
  type OverviewEvent,
  type OverviewResident,
  type ResidentAvailability,
  type WorldOverview as WorldOverviewData,
} from './api';
import './worldOverview.css';

// Labels for the backend's closed ActivityKind set (pns/models/world_state.py). Anything not
// listed falls back to its raw id rather than a guess.
const ACTIVITY_LABEL: Record<string, string> = {
  unspecified: '未记录活动',
  idle: '空闲',
  resting: '休息',
  studying: '学习',
  working_part_time: '打工',
  drawing: '画画',
  composing: '作曲',
  editing_video: '剪视频',
  online_chatting: '线上聊天',
  commuting: '在路上',
};
const activityLabel = (kind: string) => ACTIVITY_LABEL[kind] ?? kind;

const AVAILABILITY_LABEL: Record<ResidentAvailability, string> = {
  available: '醒着',
  busy: '专注中',
  asleep: '睡着',
};

// Unit ids from packs/pjsk/units. Display vocabulary only; membership comes from the backend.
const UNITS: { id: string; name: string; short: string }[] = [
  { id: 'leoneed', name: 'Leo/need', short: 'Leo/need' },
  { id: 'mmj', name: 'MORE MORE JUMP!', short: 'MMJ' },
  { id: 'vbs', name: 'Vivid BAD SQUAD', short: 'VBS' },
  { id: 'wxs', name: 'Wonderlands×Showtime', short: 'WxS' },
  { id: '25ji', name: '25時、ナイトコードで。', short: '25時' },
];
const unitShort = (id: string | null) => (id ? (UNITS.find((u) => u.id === id)?.short ?? id) : '');

const WEEKDAY = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

// A gap this long between two world events is drawn as a quiet stretch. Silence is a valid
// state, so it is shown as a fact about the world, not as a warning.
const QUIET_GAP_MINUTES = 60;

// Nodes where sharing a location id does not mean being together: the city itself, open
// streets, and `private_residence`, which stands for everyone's separate homes.
const NOT_TOGETHER = new Set(['tokyo', 'city_streets', 'private_residence']);

// How often the page re-reads the world. The clock runs on its own; this only refreshes the view.
const POLL_MS = 5000;
const EVENT_LIMIT = 200;

// Simulation clock values are naive local world time. Only the first 16 characters
// (YYYY-MM-DDTHH:MM) are used, parsed as UTC so the viewer's timezone never shifts world time.
function parseClock(value: string): Date {
  return new Date(`${value.slice(0, 16)}:00Z`);
}
function timeText(value: string): string {
  return value.slice(11, 16);
}
function dateText(value: string): string {
  const d = parseClock(value);
  return `${d.getUTCMonth() + 1}月${d.getUTCDate()}日 ${WEEKDAY[d.getUTCDay()]}`;
}
function durationText(minutes: number): string {
  const d = Math.floor(minutes / 1440);
  const h = Math.floor((minutes % 1440) / 60);
  const m = minutes % 60;
  const parts = [d ? `${d} 天` : '', h ? `${h} 小时` : '', m && !d ? `${m} 分钟` : ''].filter(Boolean);
  return parts.join(' ') || '0 分钟';
}
function minutesBetween(a: string, b: string): number {
  return Math.round((parseClock(b).getTime() - parseClock(a).getTime()) / 60000);
}
const textOf = (e: OverviewEvent) => (typeof e.payload.text === 'string' ? e.payload.text : '');

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function WorldOverview() {
  const [openWorlds, setOpenWorlds] = useState<string[] | null>(null);
  const [worldId, setWorldId] = useState<string | null>(null);
  const [data, setData] = useState<WorldOverviewData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [unitFilter, setUnitFilter] = useState<string | null>(null);
  // Only the newest request may land: an older response arriving late must not overwrite a newer one.
  const requestSeq = useRef(0);

  const loadWorlds = useCallback(async () => {
    try {
      const { worlds } = await fetchPersistentWorlds();
      const open = worlds.filter((w) => w.owned).map((w) => w.world_id);
      setOpenWorlds(open);
      setWorldId((current) => (current && open.includes(current) ? current : (open[0] ?? null)));
      if (!open.length) setData(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  const loadOverview = useCallback(
    async (id: string) => {
      const seq = ++requestSeq.current;
      try {
        const next = await fetchWorldOverview(id, EVENT_LIMIT);
        if (seq !== requestSeq.current) return;
        setData(next);
        setError(null);
      } catch (e) {
        if (seq !== requestSeq.current) return;
        if (e instanceof ApiError && e.category === 'world_not_open') {
          // Closed from the other tab or another operator: re-read which worlds are open.
          void loadWorlds();
          return;
        }
        // world_busy and transient failures: keep showing the last good view, say why it is stale.
        setError(errorText(e));
      }
    },
    [loadWorlds],
  );

  useEffect(() => {
    void loadWorlds();
  }, [loadWorlds]);

  useEffect(() => {
    if (!worldId) return;
    setData((current) => (current?.world_id === worldId ? current : null));
    void loadOverview(worldId);
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void loadOverview(worldId);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [worldId, loadOverview]);

  const locationById = useMemo(() => new Map((data?.locations ?? []).map((l) => [l.id, l])), [data]);
  const residentById = useMemo(() => new Map((data?.residents ?? []).map((r) => [r.id, r])), [data]);
  const channelName = (id: string | null) =>
    id ? (data?.channels.find((c) => c.id === id)?.name ?? id) : '';
  const locationName = (id: string | null) => (id ? (locationById.get(id)?.name ?? id) : '未知地点');
  const nameOf = (id: string | null) => (id ? (residentById.get(id)?.name ?? id) : '世界');
  const inFilter = (id: string | null) => !unitFilter || (id !== null && residentById.get(id)?.unit === unitFilter);

  // Physical grouping: everyone appears exactly once. Together (two or more in one place),
  // awake on their own, or asleep. Remote presence is grouped separately.
  const groups = useMemo(() => {
    const residents = data?.residents ?? [];
    // The building a location belongs to: the ancestor directly under the root.
    const placeOf = (locationId: string): string => {
      let cur = locationById.get(locationId);
      while (cur?.parent_id && locationById.get(cur.parent_id)?.parent_id) cur = locationById.get(cur.parent_id);
      return cur?.id ?? locationId;
    };
    const byPlace = new Map<string, OverviewResident[]>();
    for (const r of residents) {
      if (r.location_id === null || NOT_TOGETHER.has(r.location_id)) continue;
      const place = placeOf(r.location_id);
      byPlace.set(place, [...(byPlace.get(place) ?? []), r]);
    }
    const together = [...byPlace.entries()].filter(([, rs]) => rs.length >= 2);
    const grouped = new Set(together.flatMap(([, rs]) => rs.map((r) => r.id)));
    const rest = residents.filter((r) => !grouped.has(r.id));
    return {
      together,
      awake: rest.filter((r) => r.availability !== 'asleep'),
      asleep: rest.filter((r) => r.availability === 'asleep'),
    };
  }, [data, locationById]);

  const visibleEvents = useMemo(
    () =>
      (data?.events ?? []).filter((e) =>
        focus
          ? e.actor === focus
          : !unitFilter || (e.actor !== null && residentById.get(e.actor)?.unit === unitFilter),
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
    if (!data) return items;
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
  }, [visibleEvents, data]);

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
            {r.unit ? <span className="wo-unit">{unitShort(r.unit)}</span> : null}
          </span>
          <span className="wo-resident-meta">{detail}</span>
        </span>
      </button>
    </li>
  );
  const activityText = (r: OverviewResident) => `${activityLabel(r.activity.kind)}（${timeText(r.activity.since)} 起）`;

  const renderEvent = (e: OverviewEvent) => {
    const who = <strong>{nameOf(e.actor)}</strong>;
    switch (e.type) {
      case 'character.location_changed':
        return <>{who} 到了 {locationName(e.location_id)}</>;
      case 'character.activity_changed':
        return <>{who} 开始{activityLabel(String(e.payload.activity ?? ''))}</>;
      case 'presence.joined_channel':
        return <>{who} 进入 {channelName(e.channel_id)}</>;
      case 'presence.left_channel':
        return <>{who} 离开 {channelName(e.channel_id)}</>;
      case 'message.sent':
        return (
          <>
            {who} 在 {channelName(e.channel_id)}
            <span className="wo-quote">{textOf(e)}</span>
          </>
        );
      case 'dialogue.spoken':
        return (
          <>
            {who} 在 {e.channel_id ? channelName(e.channel_id) : locationName(e.location_id)} 说
            <span className="wo-quote">{textOf(e)}</span>
          </>
        );
      default:
        return <>{who} · {e.type}</>;
    }
  };

  if (openWorlds === null && !error) {
    return <div className="world-overview"><p className="wo-note">正在读取世界…</p></div>;
  }
  if (openWorlds !== null && !openWorlds.length) {
    return (
      <div className="world-overview">
        <p className="wo-note">
          本进程里没有开着的世界。去「持久世界」页开局或恢复一个，这里就会显示它此刻的样子。
        </p>
      </div>
    );
  }

  const autonomy = data?.autonomy ?? null;
  const cognitionOn = !!autonomy?.cognition_available;
  const clockState = autonomy?.clock_state;
  const residents = data?.residents ?? [];

  const together = groups.together
    .map(([place, rs]) => [place, rs.filter((r) => inFilter(r.id))] as const)
    .filter(([, rs]) => rs.length);
  const awake = groups.awake.filter((r) => inFilter(r.id));
  const asleep = groups.asleep.filter((r) => inFilter(r.id));
  const online = (data?.channels ?? [])
    .map((c) => [c, residents.filter((r) => r.channels.includes(c.id) && inFilter(r.id))] as const)
    .filter(([, rs]) => rs.length);
  const presentUnits = UNITS.filter((u) => residents.some((r) => r.unit === u.id));
  const truncated = data ? data.events.length >= EVENT_LIMIT : false;

  return (
    <div className="world-overview">
      <div className="wo-head">
        <div className="wo-clock">
          <span className="wo-time">{data ? timeText(data.clock) : '--:--'}</span>
          <span className="wo-date">{data ? `${dateText(data.clock)} · 世界时间` : '世界时间'}</span>
        </div>
        <div className="wo-state">
          <span className={`status-dot${cognitionOn ? ' running' : ''}`} />
          <span>{cognitionOn ? '认知进行中' : '认知未开启'}</span>
          {clockState && clockState !== 'healthy' ? (
            <span>· 时钟{clockState === 'catching_up' ? '追赶中' : clockState === 'faulted' ? '故障' : clockState}</span>
          ) : null}
          {openWorlds && openWorlds.length > 1 ? (
            <select value={worldId ?? ''} onChange={(ev) => setWorldId(ev.target.value)} aria-label="选择世界">
              {openWorlds.map((id) => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>
          ) : null}
          {data ? <span className="wo-mono">{data.world_id} · rev {data.revision}</span> : null}
        </div>
      </div>
      {error ? (
        <p className="wo-note" role="status">
          {data ? '这一页可能不是最新的：' : '读不到这个世界：'}
          {error}
        </p>
      ) : null}

      {data ? (
        <div className="wo-body">
          <div className="wo-side">
            {presentUnits.length > 1 ? (
              <div className="wo-filter" role="group" aria-label="按团筛选">
                <button className={`toggle${unitFilter === null ? ' on' : ''}`} onClick={() => setUnitFilter(null)}>
                  全部
                </button>
                {presentUnits.map((u) => (
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
            ) : null}

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
                          <span>{r.name}</span>
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
                  ? `${nameOf(focus)}发起的世界事件`
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
                这是世界记录里由{nameOf(focus)}发起的事件，不等于{nameOf(focus)}经历或记得的内容。
              </p>
            ) : null}
            {truncated ? (
              <p className="wo-hint">只显示最近 {EVENT_LIMIT} 条（不含时间推进）；整份世界历史共 {data.total_events} 条。</p>
            ) : null}
            {timeline.length ? (
              <ol className="wo-timeline">
                {timeline.map((item) => (
                  <Fragment key={item.key}>
                    {item.kind === 'date' ? (
                      <li className="wo-date-row">{item.label}</li>
                    ) : item.kind === 'quiet' ? (
                      <li className="wo-quiet">{durationText(item.minutes)}没有世界事件</li>
                    ) : (
                      <li className="wo-event">
                        <span className="wo-event-time">{timeText(item.event.at)}</span>
                        <span className="wo-event-text">{renderEvent(item.event)}</span>
                      </li>
                    )}
                  </Fragment>
                ))}
              </ol>
            ) : (
              <p className="wo-hint">还没有世界事件。安静本身也是一种生活状态。</p>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}
