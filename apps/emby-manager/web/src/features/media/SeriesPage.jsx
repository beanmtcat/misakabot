import { useEffect, useState } from 'react';
import { DataTable, Toolbar } from '../../components/DataTable';
import TableFooter from '../../components/TableFooter';
import { api, embyItemUrl, formatDate, formatTime } from '../../lib/api';
import './MediaPages.css';
import './TrackingStates.css';

const PAGE_SIZE = 20;
const blankDetail = { tracking: true, library_name: '', themoviedb: '', quark: '', alipan: '', alias: '', lock_season: '', index_name: '', path_map_override: '' };

function shanghaiDate() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function updateState(item) {
  const localCount = Number(item.episode_count || 0);
  if (localCount || item.official_latest) return `本服 ${localCount} / 已播 ${item.official_latest || '—'}`;
  return item.update_time ? formatTime(item.update_time) : '暂无更新信息';
}

function rowStateClass(item) {
  const nextUpdate = String(item.next_update || '').slice(0, 10);
  const today = shanghaiDate();
  const missingAired = item.is_missing_aired ?? (Number(item.episode_count) < Number(item.official_latest));
  const overdue = item.is_overdue ?? Boolean(nextUpdate && nextUpdate < today);
  const metadataStale = item.is_metadata_stale ?? (
    !nextUpdate && Number(item.total) > 0 && Number(item.official_latest) < Number(item.total)
  );
  if (Number(item.node_status) === 0) return 'series-row-danger';
  // A title may be due today and already be missing an older aired episode.
  // In that case the actionable exception wins over the yellow today marker.
  if (overdue || missingAired || metadataStale) return 'series-row-stale';
  if (Number(item.total) > 0 && Number(item.episode_count) === Number(item.total)) return 'series-row-complete';
  if (Number(item.official_latest) > 0 && Number(item.local_latest) === Number(item.official_latest) && Number(item.episode_count) === Number(item.official_latest)) return 'series-row-caught-up';
  if (item.is_today ?? nextUpdate === today) return 'series-row-today';
  return '';
}

function trackingStateNotes(item) {
  const notes = [];
  if (item.is_overdue) notes.push('播出日期过期');
  if (item.is_missing_aired) notes.push('缺已播');
  if (item.is_metadata_stale) notes.push('TMDB 后续日期缺失');
  if (item.is_today) notes.push('今日更新');
  if (!notes.length && item.is_completed_season) notes.push('本季已完结');
  return notes;
}

function seriesTitle(item) {
  const year = String(item.alias || '');
  return `${item.name || '未命名电视剧'}${/^\d{4}$/.test(year) ? ` (${year})` : ''}`;
}

function seriesIndexLetter(item) {
  const indexName = String(item.index_name || '').trim().toUpperCase();
  const letter = indexName.charAt(0);
  return /^[A-Z]$/.test(letter) ? letter : null;
}

function Dialog({ title, children, onClose, wide = false }) {
  return <div className="series-modal-backdrop" role="presentation" onMouseDown={onClose}>
    <section className={`series-modal ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-label={title} onMouseDown={(event) => event.stopPropagation()}>
      <header><h2>{title}</h2><button className="text-button" onClick={onClose} aria-label="关闭">×</button></header>
      {children}
    </section>
  </div>;
}

export default function SeriesPage({ notify, archived = false }) {
  const [series, setSeries] = useState([]);
  const [query, setQuery] = useState('');
  const [stateFilter, setStateFilter] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [todayTotal, setTodayTotal] = useState(0);
  const [exceptionTotal, setExceptionTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [trackedTotal, setTrackedTotal] = useState(0);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);
  const [trackingEnabled, setTrackingEnabled] = useState(null);
  const [moviepilotEnabled, setMoviepilotEnabled] = useState(null);
  const [libraries, setLibraries] = useState([]);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [comparison, setComparison] = useState(null);
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState(null);
  const [syncingId, setSyncingId] = useState(null);

  async function loadSyncStatus() {
    try {
      const status = await api('/v1/emby/sync-status');
      setLastSyncedAt(status.series_tracking?.last_success_at || status.series?.last_success_at || null);
      setTrackingEnabled(Boolean(status.series_tracking?.enabled));
      setMoviepilotEnabled(Boolean(status.moviepilot_subscriptions?.enabled));
    } catch { /* The list remains usable if a status refresh fails. */ }
  }

  async function loadLibraries() {
    try {
      const result = await api('/v1/emby/libraries');
      setLibraries(result.items || []);
    } catch { /* Settings retains other controls if library metadata is unavailable. */ }
  }

  async function load() {
    setLoading(true);
    try {
      const pagination = archived ? `page=${page}&size=${PAGE_SIZE}` : 'all_items=true';
      const result = await api(`/v1/emby/series?${pagination}&query=${encodeURIComponent(query)}&tracking=${archived ? 'false' : 'true'}${stateFilter ? `&state=${stateFilter}` : ''}`);
      setSeries(result.items); setTotal(result.total); setTrackedTotal(result.tracked_total || 0);
      setTodayTotal(result.today_total || 0); setExceptionTotal(result.exception_total || 0);
    } catch (reason) { notify(reason.message, true); } finally { setLoading(false); }
  }

  useEffect(() => {
    const timer = window.setTimeout(load, 180);
    return () => window.clearTimeout(timer);
  }, [page, query, archived, stateFilter]);
  useEffect(() => {
    loadSyncStatus();
    loadLibraries();
    const timer = window.setInterval(loadSyncStatus, 30000);
    return () => window.clearInterval(timer);
  }, []);

  async function syncTracking() {
    if (syncing) return;
    setSyncing(true);
    setSyncMessage(trackingEnabled || moviepilotEnabled ? '正在同步 Emby、MoviePilot 与 TMDB 数据，请勿重复点击。' : '正在同步 Emby 剧集索引，请勿重复点击。');
    try {
      if (!trackingEnabled && !moviepilotEnabled) {
        const result = await api('/v1/emby/series/sync', { method: 'POST' });
        const message = `Emby 剧集：新建 ${result.created}，更新 ${result.updated}，TMDB 合并 ${result.promoted}，单集 ${result.episodes || 0}`;
        setSyncMessage(`同步完成 · ${message}`);
        notify(message);
        load(); loadSyncStatus();
        return;
      }
      const result = await api('/v1/emby/series/tracking/sync', { method: 'POST' });
      const messages = [];
      if (result.emby_series) messages.push(`Emby 剧集：新建 ${result.emby_series.created}，TMDB 合并 ${result.emby_series.promoted}，单集 ${result.emby_series.episodes || 0}`);
      if (result.moviepilot) messages.push(`MoviePilot：恢复 ${result.moviepilot.enabled}，新建 ${result.moviepilot.created}`);
      if (result.tracking) messages.push(`TMDB：更新 ${result.tracking.updated}，失败 ${result.tracking.failed}`);
      const message = messages.join('；') || '追更同步完成';
      setSyncMessage(`同步完成 · ${message}`);
      notify(message); load(); loadSyncStatus();
    } catch (reason) {
      setSyncMessage(`同步失败 · ${reason.message}`);
      notify(reason.message, true);
    } finally {
      setSyncing(false);
    }
  }

  async function syncOneSeries(item) {
    if (syncingId || syncing) return;
    setSyncingId(item.id);
    try {
      const result = await api(`/v1/emby/series/${encodeURIComponent(item.id)}/sync`, { method: 'POST' });
      const tracking = result.tracking;
      const message = `《${seriesTitle(item)}》已同步 · 单集 ${result.episodes || 0}${tracking ? ` · TMDB 更新 ${tracking.updated || 0}` : ''}`;
      setSyncMessage(`同步完成 · ${message}`);
      notify(message); await Promise.all([load(), loadSyncStatus()]);
    } catch (reason) {
      notify(`《${seriesTitle(item)}》同步失败：${reason.message}`, true);
    } finally {
      setSyncingId(null);
    }
  }

  async function openSettings(item) {
    try {
      const detail = await api(`/v1/emby/series/${encodeURIComponent(item.id)}`);
      setEditing({ id: item.id, name: item.name, ...blankDetail, ...detail, lock_season: detail.lock_season || '' });
    } catch (reason) { notify(reason.message, true); }
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (!editing) return;
    setSaving(true);
    try {
      const { id, name, ...detail } = editing;
      await api(`/v1/emby/series/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify({ ...detail, library_name: detail.library_name || null, lock_season: detail.lock_season === '' ? null : Number(detail.lock_season) }) });
      setEditing(null); notify(`《${name}》设置已保存`); load(); loadSyncStatus();
    } catch (reason) { notify(reason.message, true); } finally { setSaving(false); }
  }

  async function openComparison(item) {
    setComparison({ item, data: null }); setComparisonLoading(true);
    try { setComparison({ item, data: await api(`/v1/emby/series/${encodeURIComponent(item.id)}/episode-comparison`) }); }
    catch (reason) { notify(reason.message, true); setComparison(null); }
    finally { setComparisonLoading(false); }
  }

  function selectDashboardState(nextState) {
    setStateFilter((currentState) => currentState === nextState && nextState ? '' : nextState);
    setPage(1);
  }

  function jumpToIndex(letter) {
    document.querySelector(`[data-series-index="${letter}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  const episodeCount = series.reduce((sum, item) => sum + Number(item.episode_count || 0), 0);
  const indexLetters = [...new Set(series.map(seriesIndexLetter).filter(Boolean))].sort();
  const sectionName = archived ? '已归档' : '追更中';
  return <section className="page-section media-section">
    {!archived && <nav className="series-filter-tabs" aria-label="追更剧集筛选"><button type="button" className={!stateFilter ? 'active' : ''} onClick={() => selectDashboardState('')} aria-pressed={!stateFilter}>追更中 <b>{trackedTotal}</b></button><button type="button" className={stateFilter === 'today' ? 'active' : ''} onClick={() => selectDashboardState('today')} aria-pressed={stateFilter === 'today'}>今日更新 <b>{todayTotal}</b></button><button type="button" className={stateFilter === 'exception' ? 'active' : ''} onClick={() => selectDashboardState('exception')} aria-pressed={stateFilter === 'exception'}>更新异常 <b>{exceptionTotal}</b></button></nav>}
    <Toolbar query={query} onQuery={(value) => { setQuery(value); setPage(1); }} filter={stateFilter} onFilter={(value) => { setStateFilter(value); setPage(1); }} filterOptions={archived ? [['', '全部已归档']] : [['', '全部追更'], ['today', '今日更新'], ['exception', '更新异常']]} onRefresh={load} onSync={!archived ? syncTracking : undefined} syncing={syncing} syncLabel={trackingEnabled || moviepilotEnabled ? '同步追更' : '同步剧集'} searchPlaceholder={`搜索${sectionName}电视剧`} />
    <DataTable headers={['剧集', '节点 / 媒体库', '当前季', '本服 / 官方已播', '更新日期', '操作']} empty={series.length === 0} loading={loading}>
      {series.map((item) => <tr key={item.id} className={rowStateClass(item)} data-series-index={seriesIndexLetter(item) || undefined}>
        <td>{Number(item.id) > 0 && item.server_id ? <a className="emby-item-link" href={embyItemUrl(item.id, item.server_id)} target="_blank" rel="noreferrer">{seriesTitle(item)}</a> : <strong>{seriesTitle(item)}</strong>}<small>{item.index_name || '—'} · ID {item.id}{item.themoviedb ? ` · TMDB ${item.themoviedb}` : ''}</small></td>
        <td><span className={`node-state ${item.node_status === 1 ? 'online' : ''}`}><i />{item.node_name}</span><small>{item.library_name}</small></td>
        <td><span className="episode-count">{item.season || (item.season_number ? `第 ${item.season_number} 季` : '季数未知')}</span><small>{item.total ? `官方共 ${item.total} 集 · 本服 ${item.episode_count || 0} 集` : `本服 ${item.episode_count || 0} 集`}</small></td>
        <td><button className="table-link" onClick={() => openComparison(item)}>{updateState(item)}</button><small>点击查看逐集对比</small></td>
        <td>{formatDate(item.next_update)}{trackingStateNotes(item).map((note) => <small key={note} className="tracking-state-note">{note}</small>)}<small>同步 {formatTime(item.mtime)}</small></td>
        <td className="action-cell"><div className="series-actions">{Number(item.id) > 0 && item.server_id && <button className="secondary compact" onClick={() => syncOneSeries(item)} disabled={Boolean(syncingId) || syncing}>{String(syncingId) === String(item.id) ? '同步中…' : '同步'}</button>}<button className="secondary compact" onClick={() => openSettings(item)} disabled={Boolean(syncingId) || syncing}>设置</button>{item.alipan && <a className="cloud-link" href={item.alipan} target="_blank" rel="noreferrer">阿里</a>}{item.quark && <a className="cloud-link quark" href={item.quark} target="_blank" rel="noreferrer">夸克</a>}</div></td>
      </tr>)}
    </DataTable>
    {!archived && indexLetters.length > 0 && <nav className="series-index-sidebar" aria-label="按剧集首字母快速定位">
      {indexLetters.map((letter) => <button key={letter} type="button" onClick={() => jumpToIndex(letter)} aria-label={`定位到 ${letter} 开头的剧集`}>{letter}</button>)}
    </nav>}
    {archived ? <TableFooter total={total} page={page} pageSize={PAGE_SIZE} lastSyncedAt={lastSyncedAt} statusText="已归档剧集不会参与自动追更" onPageChange={setPage} /> : <p className={`series-sync-status ${syncing ? 'syncing' : ''}`}>{syncing && <span className="sync-indicator" />}共 {total} 部追更剧集 · 已关联 {episodeCount} 个单集 · {syncMessage || `上次同步 ${formatTime(lastSyncedAt)}`}{trackingEnabled === false && moviepilotEnabled === false ? ' · 未配置 TMDB 或 MoviePilot' : ''}</p>}
    {editing && <Dialog title={`设置 · ${editing.name}`} onClose={() => !saving && setEditing(null)}><form className="series-form" onSubmit={saveSettings}>
      <label className="check-field"><input type="checkbox" checked={Boolean(editing.tracking)} onChange={(event) => setEditing({ ...editing, tracking: event.target.checked })} />追更</label>
      <label>媒体分类<select value={editing.library_name || ''} onChange={(event) => setEditing({ ...editing, library_name: event.target.value })}>{libraries.map((library) => <option key={library.name} value={library.name}>{library.name}</option>)}</select></label>
      <label>TMDB ID<input value={editing.themoviedb || ''} onChange={(event) => setEditing({ ...editing, themoviedb: event.target.value })} /></label><label>刮削年份<input value={editing.alias || ''} readOnly /></label><label>前缀<input value={editing.index_name || ''} onChange={(event) => setEditing({ ...editing, index_name: event.target.value })} /></label>
      <label>季集锁定<input type="number" min="1" max="999" value={editing.lock_season} onChange={(event) => setEditing({ ...editing, lock_season: event.target.value })} /></label>
      <label>阿里云盘<input type="url" value={editing.alipan || ''} onChange={(event) => setEditing({ ...editing, alipan: event.target.value })} /></label><label>夸克网盘<input type="url" value={editing.quark || ''} onChange={(event) => setEditing({ ...editing, quark: event.target.value })} /></label>
      <label className="path-map-field">手工路径映射<textarea value={editing.path_map_override || ''} onChange={(event) => setEditing({ ...editing, path_map_override: event.target.value })} placeholder="华语剧集/旧名称 (2026)/|/data/media/tv/华语剧集/新刮削名称 (2026)/" /><small>填写后覆盖该剧自动映射；清空则恢复自动规则。</small></label>
      <footer><button type="button" className="secondary" onClick={() => setEditing(null)} disabled={saving}>取消</button><button disabled={saving}>{saving ? '保存中…' : '保存设置'}</button></footer>
    </form></Dialog>}
    {comparison && <Dialog title={`逐集对比 · ${comparison.item.name}`} wide onClose={() => setComparison(null)}>{comparisonLoading ? <p className="modal-loading">正在读取逐集信息…</p> : <div className="comparison-wrap"><p>第 {comparison.data?.season_number || '—'} 季</p><table className="comparison-table"><thead><tr><th>集数</th><th>状态</th><th>本服名称</th><th>TMDB 名称</th><th>本服入库</th><th>TMDB 播出</th></tr></thead><tbody>{comparison.data?.comparison?.length ? comparison.data.comparison.map((episode) => <tr key={episode.episode_number} className={episode.has_server && episode.has_tmdb ? 'complete' : episode.has_server ? 'server-only' : 'missing'}><td>第 {episode.episode_number} 集</td><td>{episode.has_server && episode.has_tmdb ? '✓' : episode.has_server ? '本服' : '缺失'}</td><td>{episode.server?.name || '—'}</td><td>{episode.tmdb?.name || '—'}</td><td>{formatDate(episode.server?.date_created)}</td><td>{formatDate(episode.tmdb?.air_date)}</td></tr>) : <tr><td colSpan="6">当前季没有可对比的剧集数据。</td></tr>}</tbody></table></div>}</Dialog>}
  </section>;
}
