import { useEffect, useState } from 'react';
import { api, formatBytes, formatTime } from '../../lib/api';
import '../media/MediaPages.css';
import './HomePage.css';

const emptyDashboard = {
  counts: { users: 0, movies: 0, series: 0, episodes: 0, watching: 0 },
  recent_movies: [],
  recent_episodes: [],
  traffic: { today: {}, month: {}, recent: [] },
};

function curvePath(items, key, peak) {
  const width = 640;
  const height = 84;
  const points = items.map((item, index) => ({
    x: items.length === 1 ? width / 2 : (index / (items.length - 1)) * width,
    y: height - ((Number(item[key]) || 0) / peak) * height,
  }));
  if (points.length === 1) return `M 0 ${points[0].y} L ${width} ${points[0].y}`;
  return points.reduce((path, point, index) => {
    if (index === 0) return `M ${point.x} ${point.y}`;
    const previous = points[index - 1];
    const beforePrevious = points[index - 2] || previous;
    const next = points[index + 1] || point;
    const controlOneX = previous.x + (point.x - beforePrevious.x) / 6;
    const controlOneY = previous.y + (point.y - beforePrevious.y) / 6;
    const controlTwoX = point.x - (next.x - previous.x) / 6;
    const controlTwoY = point.y - (next.y - previous.y) / 6;
    return `${path} C ${controlOneX} ${controlOneY}, ${controlTwoX} ${controlTwoY}, ${point.x} ${point.y}`;
  }, '');
}

export default function HomePage({ notify, onNavigate }) {
  const [dashboard, setDashboard] = useState(emptyDashboard);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    try {
      setDashboard(await api('/v1/emby/dashboard'));
    } catch (reason) {
      notify(reason.message, true);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);
  const { counts, recent_movies: movies, recent_episodes: episodes, traffic = emptyDashboard.traffic } = dashboard;
  const metrics = [
    ['电影', counts.movies, '当前有效电影'],
    ['电视剧', counts.series, '当前有效剧集'],
    ['追更中', counts.tracking, '已启用追更标记'],
    ['单集', counts.episodes, '已入库的剧集单元'],
    ['用户', counts.users, '已同步 Emby 用户'],
    ['观看中', counts.watching, '当前活跃播放会话'],
  ];
  const trafficDays = traffic.recent || [];
  const trafficPeak = Math.max(1, ...trafficDays.flatMap((item) => [Number(item.rx_bytes) || 0, Number(item.tx_bytes) || 0]));

  return <section className="page-section media-section">
    <div className="metric-grid home-metrics">
      {metrics.map(([label, value, hint]) => <article key={label}><span>{label}</span><strong>{loading ? '—' : value || 0}</strong><small>{hint}</small></article>)}
    </div>
    <section className="traffic-panel">
      <header><div><h2>服务器流量</h2><p>基于服务器网络接口的每日汇总</p></div><small>更新 {formatTime(traffic.today?.updated_at)}</small></header>
      <div className="traffic-summary">
        <article><span>今日总流量</span><strong>{formatBytes(traffic.today?.total_bytes)}</strong><small>↓ {formatBytes(traffic.today?.rx_bytes)}　↑ {formatBytes(traffic.today?.tx_bytes)}</small></article>
        <article><span>本月总流量</span><strong>{formatBytes(traffic.month?.total_bytes)}</strong><small>↓ {formatBytes(traffic.month?.rx_bytes)}　↑ {formatBytes(traffic.month?.tx_bytes)}</small></article>
        <article><span>今日平均速率</span><strong>{Number(traffic.today?.avg_rate || 0).toFixed(2)} Mbps</strong><small>收发接口平均值</small></article>
      </div>
      <div className="traffic-trend" aria-label="最近七日流量趋势">
        {trafficDays.length ? <div className="traffic-chart"><div className="traffic-scale"><span>{formatBytes(trafficPeak)}</span><span>{formatBytes(trafficPeak / 2)}</span><span>0 B</span></div><div className="traffic-plot"><svg viewBox="0 0 640 84" preserveAspectRatio="none" role="img" aria-label="下载和上传流量曲线"><path className="traffic-gridline" d="M 0 21 H 640 M 0 42 H 640 M 0 63 H 640" /><path className="traffic-curve traffic-curve-rx" d={curvePath(trafficDays, 'rx_bytes', trafficPeak)} /><path className="traffic-curve traffic-curve-tx" d={curvePath(trafficDays, 'tx_bytes', trafficPeak)} /></svg><div className="traffic-axis">{trafficDays.map((item) => <span key={item.date} title={`${item.date}：${formatBytes(item.total_bytes)}`}>{String(item.date).slice(5)}</span>)}</div></div></div> : <p className="traffic-empty">暂无服务器流量数据</p>}
      </div>
      <footer><span><i className="traffic-key rx" />下载</span><span><i className="traffic-key tx" />上传</span></footer>
    </section>
    <div className="overview-actions">
      <div><h2>媒体库概览</h2><p>内容列表来自已有的 Emby 媒体索引，观看状态每分钟自动更新。</p></div>
      <button className="secondary compact" onClick={load}>↻ 刷新概览</button>
    </div>
    <div className="library-grid">
      <section className="library-card">
        <header><h2><span>▸</span>最新电影</h2><button className="link-button" onClick={() => onNavigate('movies')}>全部电影 →</button></header>
        <div className="library-list">
          {movies.length ? movies.map((movie) => <article key={movie.id}><span className="media-mark">MOV</span><span><strong>{movie.name || '未命名电影'}</strong><small>{[movie.container, formatBytes(movie.size)].filter(Boolean).join(' · ') || '媒体文件'}</small></span><time>{formatTime(movie.date_created)}</time></article>) : <p className="library-empty">暂无电影索引数据</p>}
        </div>
      </section>
      <section className="library-card">
        <header><h2><span>▸</span>最近入库单集</h2><button className="link-button" onClick={() => onNavigate('series-following')}>追更列表 →</button></header>
        <div className="library-list">
          {episodes.length ? episodes.map((episode) => <article key={episode.id}><span className="media-mark">EP</span><span><strong>{episode.series_name || '未归类电视剧'}{/^\d{4}$/.test(String(episode.scraped_year || '')) ? ` (${episode.scraped_year})` : ''} · {episode.episode_name || '未命名单集'}</strong><small>第 {episode.season_number || 0} 季 · 第 {episode.episode_number || 0} 集</small></span><time>{formatTime(episode.date_created)}</time></article>) : <p className="library-empty">暂无剧集索引数据</p>}
        </div>
      </section>
    </div>
  </section>;
}
