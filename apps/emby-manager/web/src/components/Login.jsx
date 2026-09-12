import { useState } from 'react';
import { api } from '../lib/api';

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const session = await api('/auth/login', {
        method: 'POST', body: JSON.stringify({ username, password }),
      });
      onLogin(session);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setBusy(false);
    }
  }

  return <main className="login-shell"><section className="login-intro"><div className="brand"><span className="brand-mark">E</span><span>EMBY<span className="brand-muted">.OPS</span></span></div><div><p className="eyebrow">MEDIA OPERATIONS</p><h1>让媒体服务<br />井然有序。</h1><p>集中管理成员访问权限，快速掌握正在发生的播放活动。</p></div><span className="login-orbit orbit-one" /><span className="login-orbit orbit-two" /></section>
    <section className="login-panel"><div className="login-card"><p className="eyebrow">受限访问</p><h2>登录管理台</h2><p className="muted intro">使用 Dragonli 管理员账号继续。</p>
      <form onSubmit={submit}>
        <label>用户名<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="输入用户名" required /></label>
        <label>密码<input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" placeholder="输入密码" required /></label>
        <button disabled={busy}>{busy ? '正在验证…' : '进入工作台'}<span>→</span></button>
        {error && <p className="error">{error}</p>}
      </form>
    </div></section></main>;
}
