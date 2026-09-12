import { useEffect, useState } from 'react';
import { DataTable, Toolbar } from '../../components/DataTable';
import TableFooter from '../../components/TableFooter';
import { api, embyItemUrl, formatBytes, formatDuration, formatTicks, formatTime } from '../../lib/api';
import './MediaPages.css';

const PAGE_SIZE = 20;

function Dialog({ title, children, onClose }) {
  return <div className="series-modal-backdrop" role="presentation" onMouseDown={onClose}>
    <section className="series-modal wide movie-history-modal" role="dialog" aria-modal="true" aria-label={title} onMouseDown={(event) => event.stopPropagation()}>
      <header><h2>{title}</h2><button className="text-button" onClick={onClose} aria-label="关闭">×</button></header>
      {children}
    </section>
  </div>;
}

export default function MoviesPage({ notify }) {
  const [movies, setMovies] = useState([]);
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [movieLogs, setMovieLogs] = useState(null);

  async function load() {
    setLoading(true);
    try {
      const result = await api(`/v1/emby/movies?page=${page}&size=${PAGE_SIZE}&query=${encodeURIComponent(query)}`);
      setMovies(result.items);
      setTotal(result.total);
    } catch (reason) {
      notify(reason.message, true);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const timer = window.setTimeout(load, 180);
    return () => window.clearTimeout(timer);
  }, [page, query]);

  async function loadMovieLogs(movie, logPage = 1) {
    setMovieLogs((current) => ({ movie, items: current?.movie.id === movie.id ? current.items : [], total: current?.movie.id === movie.id ? current.total : 0, page: logPage, loading: true }));
    try {
      const result = await api(`/v1/emby/watch-logs?page=${logPage}&size=${PAGE_SIZE}&item_type=Movie&item_id=${encodeURIComponent(movie.id)}`);
      setMovieLogs({ movie, items: result.items, total: result.total, page: result.page, loading: false });
    } catch (reason) {
      notify(reason.message, true);
      setMovieLogs(null);
    }
  }

  function openMovieLogs(movie) {
    loadMovieLogs(movie);
  }

  const watched = movies.filter((movie) => Number(movie.view_count) > 0).length;
  return <section className="page-section media-section">
    <div className="metric-grid"><article><span>电影总数</span><strong>{total}</strong><small>当前有效媒体索引</small></article><article><span>本页已播放</span><strong>{watched}</strong><small>有观看记录的电影</small></article><article><span>本页未播放</span><strong>{movies.length - watched}</strong><small>等待第一次观看</small></article></div>
    <Toolbar query={query} onQuery={(value) => { setQuery(value); setPage(1); }} onRefresh={load} searchPlaceholder="搜索电影名称" />
    <DataTable headers={['电影', '时长', '文件', '观看次数', '最近观看', '入库时间', '操作']} empty={movies.length === 0} loading={loading}>
      {movies.map((movie) => <tr key={movie.id}><td>{movie.server_id ? <a className="emby-item-link" href={embyItemUrl(movie.id, movie.server_id)} target="_blank" rel="noreferrer">{movie.name || '未命名电影'}</a> : <strong>{movie.name || '未命名电影'}</strong>}<small>{movie.media_type || 'Movie'}</small></td><td>{formatTicks(movie.runtime_ticks)}</td><td><span className="size-value">{movie.container || '—'}</span><small>{formatBytes(movie.size)}</small></td><td><span className={`pill ${Number(movie.view_count) ? 'success' : ''}`}>{movie.view_count || 0} 次</span></td><td>{formatTime(movie.last_played)}</td><td>{formatTime(movie.date_created)}</td><td className="action-cell"><button className="secondary compact" onClick={() => openMovieLogs(movie)}>观看记录</button></td></tr>)}
    </DataTable>
    <TableFooter total={total} page={page} pageSize={PAGE_SIZE} statusText="媒体索引保持原有库表结构" onPageChange={setPage} />
    {movieLogs && <Dialog title={`观看记录 · ${movieLogs.movie.name || '未命名电影'}`} onClose={() => setMovieLogs(null)}>
      {movieLogs.loading ? <p className="modal-loading">正在读取观看记录…</p> : <div className="movie-history-wrap"><table className="movie-history-table"><thead><tr><th>用户</th><th>开始时间</th><th>进度</th><th>设备</th><th>IP 地址</th><th>状态</th></tr></thead><tbody>{movieLogs.items.length ? movieLogs.items.map((item) => { const progress = Math.min(100, Math.max(0, Math.round(Number(item.play_progress || 0) * 100))); return <tr key={item.id}><td>{item.username}</td><td>{formatTime(item.play_start_time)}</td><td>{progress}%<small>{formatDuration(item.play_position)} / {formatDuration(item.total_duration)}</small></td><td>{item.device_name || '未知设备'}<small>{item.client_name || 'Emby Client'}</small></td><td>{item.ip_address || '—'}</td><td><span className={`pill ${item.play_end_time ? item.is_completed ? 'success' : 'danger' : 'watching'}`}>{item.play_end_time ? item.is_completed ? '已完成' : '已结束' : '观看中'}</span></td></tr>; }) : <tr><td colSpan="6">暂无已同步的观看记录。</td></tr>}</tbody></table><TableFooter total={movieLogs.total} page={movieLogs.page} pageSize={PAGE_SIZE} onPageChange={(nextPage) => loadMovieLogs(movieLogs.movie, nextPage)} /></div>}
    </Dialog>}
  </section>;
}
