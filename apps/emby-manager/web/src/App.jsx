import { useEffect, useState } from 'react';
import Login from './components/Login';
import { api, setCsrfToken } from './lib/api';
import UsersPage from './features/users/UsersPage';
import WatchLogsPage from './features/watch/WatchLogsPage';
import LoginLogsPage from './features/login/LoginLogsPage';
import HomePage from './features/home/HomePage';
import MoviesPage from './features/media/MoviesPage';
import SeriesPage from './features/media/SeriesPage';
import { ConfirmProvider } from './components/ConfirmDialog';
import './theme.css';
import './layout-compact.css';

const navigation = [
  { id: 'home', label: '总览', description: '掌握媒体库与播放活动' },
  { id: 'users', label: '用户目录', description: '管理 Emby 访问权限' },
  { id: 'login', label: '登录日志', description: '查看 Emby 会话登录' },
  { id: 'watch', label: '播放活动', description: '查看当前与历史会话' },
  { id: 'movies', label: '电影', description: '浏览电影库与观看热度' },
  {
    id: 'series', label: '电视剧', description: '追更与归档管理',
    children: [
      { id: 'series-following', label: '追更中' },
      { id: 'series-archived', label: '已归档' },
    ],
  },
];

const pageIds = new Set(navigation.flatMap((item) => item.children || [item]).map((item) => item.id));

function pageFromLocation() {
  const requestedPage = new URLSearchParams(window.location.search).get('page');
  return requestedPage && pageIds.has(requestedPage) ? requestedPage : 'home';
}

export default function App() {
  return <ConfirmProvider><AppContent /></ConfirmProvider>;
}

function AppContent() {
  const [username, setUsername] = useState(null);
  const [page, setPage] = useState(pageFromLocation);
  const [notice, setNotice] = useState(null);
  const [theme, setTheme] = useState(() => window.localStorage.getItem('emby-manager-theme') || 'dark');
  function notify(message, error = false) { setNotice({ message, error }); window.setTimeout(() => setNotice(null), 3000); }
  useEffect(() => { api('/auth/session').then((session) => { setCsrfToken(session.csrf_token); setUsername(session.username); }).catch(() => { setCsrfToken(''); setUsername(false); }); }, []);
  useEffect(() => { document.documentElement.dataset.theme = theme; window.localStorage.setItem('emby-manager-theme', theme); }, [theme]);
  useEffect(() => {
    const url = new URL(window.location.href);
    if (page === 'home') url.searchParams.delete('page');
    else url.searchParams.set('page', page);
    window.history.replaceState({ page }, '', `${url.pathname}${url.search}${url.hash}`);
  }, [page]);
  useEffect(() => {
    const restorePage = () => setPage(pageFromLocation());
    window.addEventListener('popstate', restorePage);
    return () => window.removeEventListener('popstate', restorePage);
  }, []);
  async function logout() { await api('/auth/logout', { method: 'POST' }); setCsrfToken(''); setUsername(false); }
  if (username === null) return <main className="login-shell"><p className="loading-copy">正在验证访问权限…</p></main>;
  if (!username) return <Login onLogin={(session) => { setCsrfToken(session.csrf_token); setUsername(session.username); }} />;
  const active = navigation.flatMap((item) => item.children || [item]).find((item) => item.id === page);
  return <main className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">E</span><span>EMBY<span className="brand-muted">.OPS</span></span></div>
      <div className="sidebar-label">工作台</div>
      <nav className="sidebar-nav" aria-label="管理导航">
        {navigation.map((item, index) => item.children ? <div className="series-nav-group" key={item.id}>
          <button className={page.startsWith('series-') ? 'nav-item has-active' : 'nav-item'} onClick={() => setPage('series-following')}>
            <span className="nav-index">0{index + 1}</span><span><strong>{item.label}</strong><small>{item.description}</small></span>
          </button>
          <div className="nav-children">{item.children.map((child) => <button key={child.id} className={page === child.id ? 'nav-subitem active' : 'nav-subitem'} onClick={() => setPage(child.id)}>{child.label}</button>)}</div>
        </div> : <button key={item.id} className={page === item.id ? 'nav-item active' : 'nav-item'} onClick={() => setPage(item.id)}>
          <span className="nav-index">0{index + 1}</span><span><strong>{item.label}</strong><small>{item.description}</small></span>
        </button>)}
      </nav>
      <div className="sidebar-footer"><span className="status-dot" />服务已连接</div>
    </aside>
    <section className="workspace">
      <header className="topbar"><div className="breadcrumb"><span>媒体运营</span><b>/</b><strong>{active?.label}</strong></div><div className="account"><button className="secondary compact theme-toggle" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} title={theme === 'dark' ? '切换到明亮模式' : '切换到暗色模式'}>{theme === 'dark' ? '☀ 开灯' : '◐ 夜间'}</button><div className="avatar">{username.slice(0, 1).toUpperCase()}</div><span>{username}</span><button className="text-button" onClick={logout}>退出</button></div></header>
      <div className={page === 'movies' || page.startsWith('series-') ? 'content media-content' : 'content'}>
      {page === 'home' && <HomePage notify={notify} onNavigate={setPage} />}
      {page === 'users' && <UsersPage notify={notify} />}
      {page === 'login' && <LoginLogsPage notify={notify} />}
      {page === 'watch' && <WatchLogsPage notify={notify} />}
      {page === 'movies' && <MoviesPage notify={notify} />}
      {page === 'series-following' && <SeriesPage key="following" notify={notify} />}
      {page === 'series-archived' && <SeriesPage key="archived" notify={notify} archived />}</div>
    </section>
    {notice && <div className={`notice ${notice.error ? 'error-notice' : ''}`}>{notice.message}</div>}
  </main>;
}
