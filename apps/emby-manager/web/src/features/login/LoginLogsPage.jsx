import { useEffect, useState } from 'react';
import { api, formatTime } from '../../lib/api';
import { DataTable, Toolbar } from '../../components/DataTable';
import TableFooter from '../../components/TableFooter';

const PAGE_SIZE = 20;

export default function LoginLogsPage({ notify }) {
  const [items, setItems] = useState([]);
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);

  async function loadSyncStatus() {
    try {
      const status = await api('/v1/emby/sync-status');
      setLastSyncedAt(status.login_logs?.last_success_at || null);
    } catch { /* The list still works while status is unavailable. */ }
  }

  async function load() {
    setLoading(true);
    try {
      const data = await api(`/v1/emby/login-logs?page=${page}&size=${PAGE_SIZE}&query=${encodeURIComponent(query)}`);
      setItems(data.items);
      setTotal(data.total);
    } catch (reason) {
      notify(reason.message, true);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const delay = window.setTimeout(load, 180);
    return () => window.clearTimeout(delay);
  }, [page, query]);
  useEffect(() => {
    loadSyncStatus();
    const timer = window.setInterval(loadSyncStatus, 30000);
    return () => window.clearInterval(timer);
  }, []);

  async function sync() {
    try {
      const result = await api('/v1/emby/login-logs/sync', { method: 'POST' });
      notify(`新增 ${result.created} 条，跳过 ${result.skipped} 条现有会话`);
      load();
      loadSyncStatus();
    } catch (reason) {
      notify(reason.message, true);
    }
  }

  const successful = items.filter((item) => item.is_success).length;
  const users = new Set(items.map((item) => item.user_id)).size;
  return <section className="page-section">
    <div className="metric-grid">
      <article><span>本页登录</span><strong>{items.length}</strong><small>当前页的会话记录</small></article>
      <article><span>登录成功</span><strong>{successful}</strong><small>本页成功登录记录</small></article>
      <article><span>涉及用户</span><strong>{users}</strong><small>本页不同 Emby 用户</small></article>
    </div>
    <Toolbar
      query={query}
      onQuery={(value) => { setQuery(value); setPage(1); }}
      onRefresh={load}
      onSync={sync}
      syncLabel="同步登录"
      searchPlaceholder="搜索用户、IP 或设备"
    />
    <DataTable headers={['用户', '登录时间', 'IP 地址', '设备', '客户端', '状态']} empty={items.length === 0} loading={loading}>
      {items.map((item) => <tr key={item.id}>
        <td><div className="identity"><span className="user-avatar">{item.username.slice(0, 1).toUpperCase()}</span><strong>{item.username}</strong></div></td>
        <td>{formatTime(item.login_time)}</td>
        <td>{item.ip_address || '—'}</td>
        <td>{item.device_name || '未知设备'}</td>
        <td>{item.client_name || 'Emby Client'}</td>
        <td><span className={`pill ${item.is_success ? 'success' : 'danger'}`}>{item.is_success ? '登录成功' : '登录失败'}</span></td>
      </tr>)}
    </DataTable>
    <TableFooter total={total} page={page} pageSize={PAGE_SIZE} lastSyncedAt={lastSyncedAt} onPageChange={setPage} />
  </section>;
}
