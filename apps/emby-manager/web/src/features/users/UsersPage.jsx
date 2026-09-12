import { useEffect, useState } from 'react';
import { api, formatTime } from '../../lib/api';
import { DataTable, Toolbar } from '../../components/DataTable';
import TableFooter from '../../components/TableFooter';
import { useConfirm } from '../../components/ConfirmDialog';

const PAGE_SIZE = 20;

export default function UsersPage({ notify }) {
  const confirm = useConfirm();
  const [users, setUsers] = useState([]);
  const [query, setQuery] = useState('');
  const [disabled, setDisabled] = useState('');
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [lastSyncedAt, setLastSyncedAt] = useState(null);

  async function loadSyncStatus() {
    try {
      const status = await api('/v1/emby/sync-status');
      setLastSyncedAt(status.users.last_success_at);
    } catch { /* The list remains usable if the status request is temporarily unavailable. */ }
  }

  async function load() {
    setLoading(true);
    try {
      const status = disabled === '' ? '' : `&is_disabled=${disabled}`;
      const data = await api(`/v1/emby/users?page=${page}&size=${PAGE_SIZE}&query=${encodeURIComponent(query)}${status}`);
      setUsers(data.items);
      setTotal(data.total);
    } catch (reason) { notify(reason.message, true); } finally { setLoading(false); }
  }
  useEffect(() => {
    const delay = window.setTimeout(load, 180);
    return () => window.clearTimeout(delay);
  }, [page, query, disabled]);
  useEffect(() => {
    loadSyncStatus();
    const timer = window.setInterval(loadSyncStatus, 30000);
    return () => window.clearInterval(timer);
  }, []);

  async function toggle(user) {
    const isDisabled = !user.is_disabled;
    if (!await confirm({
      title: isDisabled ? '限制用户访问' : '恢复用户访问',
      description: `${isDisabled ? '禁用后该用户将不能访问 Emby。' : '该用户将恢复正常访问 Emby。'} 用户：${user.username}`,
      confirmLabel: isDisabled ? '确认限制' : '确认恢复',
    })) return;
    try {
      await api(`/v1/emby/users/${encodeURIComponent(user.id)}/policy`, {
        method: 'PATCH', body: JSON.stringify({ is_disabled: isDisabled }),
      });
      notify(`${user.username} 已${isDisabled ? '禁用' : '启用'}`);
      load(); loadSyncStatus();
    } catch (reason) { notify(reason.message, true); }
  }
  async function sync() {
    try {
      const result = await api('/v1/emby/users/sync', { method: 'POST' });
      notify(`已同步 ${result.synced} 位用户，跳过 ${result.skipped} 位`); load(); loadSyncStatus();
    } catch (reason) { notify(reason.message, true); }
  }
  const activeUsers = users.filter((user) => !user.is_disabled).length;
  return <section className="page-section"><div className="metric-grid"><article><span>筛选结果</span><strong>{total}</strong><small>已同步的用户</small></article><article><span>本页正常</span><strong>{activeUsers}</strong><small>可正常使用媒体库</small></article><article><span>本页受限</span><strong>{users.length - activeUsers}</strong><small>禁用状态的账户</small></article></div><Toolbar query={query} onQuery={(value) => { setQuery(value); setPage(1); }} filter={disabled} onFilter={(value) => { setDisabled(value); setPage(1); }}
    filterOptions={[['', '全部状态'], ['false', '已启用'], ['true', '已禁用']]}
    onRefresh={load} onSync={sync} syncLabel="同步用户" searchPlaceholder="搜索用户或 ID" />
    <DataTable headers={['用户', '最后活动', '远程访问', '状态', '']} empty={users.length === 0} loading={loading}>
      {users.map((user) => <tr key={user.id}><td><div className="identity"><span className="user-avatar">{user.username.slice(0, 1).toUpperCase()}</span><span><strong>{user.username}</strong><small>{user.compact_id}</small></span></div></td>
        <td>{formatTime(user.last_activity || user.last_login)}</td><td><span className={user.enable_remote_access ? 'access-state allowed' : 'access-state'}><i />{user.enable_remote_access ? '允许' : '已关闭'}</span></td>
        <td><span className={`pill ${user.is_disabled ? 'danger' : 'success'}`}>{user.is_disabled ? '已禁用' : '正常'}</span></td>
        <td className="action-cell"><button className="secondary compact" onClick={() => toggle(user)}>{user.is_disabled ? '恢复访问' : '限制访问'}</button></td></tr>)}
    </DataTable>
    <TableFooter total={total} page={page} pageSize={PAGE_SIZE} lastSyncedAt={lastSyncedAt} onPageChange={setPage} />
  </section>;
}
