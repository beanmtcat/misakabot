import { useEffect, useState } from 'react';
import { api, formatDuration, formatTime } from '../../lib/api';
import { DataTable, Toolbar } from '../../components/DataTable';
import TableFooter from '../../components/TableFooter';

const PAGE_SIZE = 20;

export default function WatchLogsPage({ notify }) {
  const [items, setItems] = useState([]);
  const [query, setQuery] = useState('');
  const [itemType, setItemType] = useState('');
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);

  async function loadSyncStatus() {
    try {
      const status = await api('/v1/emby/sync-status');
      setLastSyncedAt(status.watch_logs.last_success_at);
    } catch { /* The list remains usable if the status request is temporarily unavailable. */ }
  }

  async function load() {
    setLoading(true);
    try {
      const data = await api(`/v1/emby/watch-logs?page=${page}&size=${PAGE_SIZE}&query=${encodeURIComponent(query)}&item_type=${encodeURIComponent(itemType)}`);
      setItems(data.items);
      setTotal(data.total);
    } catch (reason) { notify(reason.message, true); } finally { setLoading(false); }
  }
  useEffect(() => {
    const delay = window.setTimeout(load, 180);
    return () => window.clearTimeout(delay);
  }, [page, query, itemType]);
  useEffect(() => {
    loadSyncStatus();
    const timer = window.setInterval(loadSyncStatus, 30000);
    return () => window.clearInterval(timer);
  }, []);
  async function sync() {
    try {
      const result = await api('/v1/emby/watch-logs/sync', { method: 'POST' });
      notify(`新增 ${result.created}，更新 ${result.updated}，结束 ${result.ended}`); load(); loadSyncStatus();
    } catch (reason) { notify(reason.message, true); }
  }
  const watching = items.filter((item) => !item.play_end_time).length;
  const completed = items.filter((item) => item.is_completed).length;
  return <section className="page-section"><div className="metric-grid"><article><span>本页会话</span><strong>{watching}</strong><small>正在播放的内容</small></article><article><span>本页完成</span><strong>{completed}</strong><small>已完成的播放记录</small></article><article><span>筛选结果</span><strong>{total}</strong><small>全部符合条件的记录</small></article></div><Toolbar query={query} onQuery={(value) => { setQuery(value); setPage(1); }} filter={itemType} onFilter={(value) => { setItemType(value); setPage(1); }}
    filterOptions={[['', '全部类型'], ['Movie', '电影'], ['Episode', '剧集']]}
    onRefresh={load} onSync={sync} syncLabel="同步活动" searchPlaceholder="搜索内容、用户或设备" />
    <DataTable headers={['用户', '内容', '进度', '设备', 'IP 地址', '开始时间', '状态']} empty={items.length === 0} loading={loading}>
      {items.map((item) => { const progress = Math.min(100, Math.max(0, Math.round((item.play_progress || 0) * 100))); return <tr key={item.id}><td><div className="identity"><span className="user-avatar">{item.username.slice(0, 1).toUpperCase()}</span><strong>{item.username}</strong></div></td>
        <td><strong>{item.item_name}</strong><small>{item.item_type || '其他内容'}</small></td><td><div className="progress-label"><span>{progress}%</span><small>{formatDuration(item.play_position)} / {formatDuration(item.total_duration)}</small></div><div className="progress-track"><i style={{ width: `${progress}%` }} /></div></td>
        <td>{item.device_name || '未知设备'}<small>{item.client_name || 'Emby Client'}</small></td><td>{item.ip_address || '—'}</td><td>{formatTime(item.play_start_time)}</td>
        <td><span className={`pill ${item.play_end_time ? item.is_completed ? 'success' : 'danger' : 'watching'}`}>{item.play_end_time ? item.is_completed ? '已完成' : '已结束' : '观看中'}</span></td></tr>; })}
    </DataTable>
    <TableFooter total={total} page={page} pageSize={PAGE_SIZE} lastSyncedAt={lastSyncedAt} onPageChange={setPage} />
  </section>;
}
