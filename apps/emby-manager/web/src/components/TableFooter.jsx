import { formatTime } from '../lib/api';
import './TableFooter.css';

export default function TableFooter({ total, page, pageSize, lastSyncedAt, statusText, onPageChange }) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const firstItem = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const lastItem = Math.min(page * pageSize, total);
  const syncLabel = statusText || '上次同步：';
  const syncValue = statusText ? null : lastSyncedAt ? formatTime(lastSyncedAt) : '等待首次同步';
  return <footer className="table-footer">
    <div className="sync-time"><span className="status-dot" />{syncLabel}{syncValue && <strong>{syncValue}</strong>}</div>
    <div className="page-controls"><span>{total === 0 ? '暂无记录' : `${firstItem}–${lastItem} / ${total} 条`}</span><button className="secondary compact" disabled={page <= 1} onClick={() => onPageChange(page - 1)}>上一页</button><span className="page-number">{page} / {totalPages}</span><button className="secondary compact" disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}>下一页</button></div>
  </footer>;
}
