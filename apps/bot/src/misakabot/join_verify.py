from __future__ import annotations

import html
import logging

import httpx

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class TurnstileVerifier:
    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key

    async def verify(self, token: str, remote_ip: str | None = None) -> bool:
        if not self.secret_key or not token:
            return False
        payload: dict[str, str] = {"secret": self.secret_key, "response": token}
        if remote_ip:
            payload["remoteip"] = remote_ip
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(TURNSTILE_VERIFY_URL, data=payload)
            response.raise_for_status()
        result = response.json()
        success = result.get("success") is True
        if not success:
            logger.warning("turnstile.rejected error_codes=%s", result.get("error-codes", []))
        return success


def join_verify_page(site_key: str) -> str:
    """Small standalone Telegram Mini App; the site key is public by design."""
    escaped_site_key = html.escape(site_key, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>入群安全验证</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer onerror="window.turnstileScriptFailed = true"></script>
<style>
  :root {{
    color-scheme: light dark;
    --bg: var(--tg-theme-secondary-bg-color, #f4f5f7);
    --surface: var(--tg-theme-bg-color, #fff);
    --text: var(--tg-theme-text-color, #17191d);
    --subtle: var(--tg-theme-hint-color, #7b818a);
    --accent: var(--tg-theme-button-color, #e64888);
    --accent-faint: color-mix(in srgb, var(--accent) 12%, transparent);
    --stroke: color-mix(in srgb, var(--text) 10%, transparent);
    --success: #20a77b;
    --danger: #e24b55;
  }}
  * {{ box-sizing: border-box; }}
  body {{ min-height: 100vh; margin: 0; color: var(--text); background: var(--bg); font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }}
  main {{ max-width: 440px; min-height: 100vh; margin: 0 auto; padding: max(28px, env(safe-area-inset-top)) 22px max(24px, env(safe-area-inset-bottom)); }}
  .top {{ display: flex; align-items: center; gap: 9px; color: var(--subtle); font-size: 13px; font-weight: 650; letter-spacing: .025em; }}
  .top-mark {{ width: 25px; height: 25px; display: grid; place-items: center; color: #fff; border-radius: 8px; background: var(--accent); box-shadow: 0 5px 13px color-mix(in srgb, var(--accent) 26%, transparent); }}
  .top-mark svg {{ width: 15px; height: 15px; }}
  .hero {{ padding: 56px 4px 31px; }}
  .eyebrow {{ display: inline-flex; align-items: center; gap: 7px; margin-bottom: 14px; color: var(--accent); font-size: 12px; font-weight: 750; letter-spacing: .08em; }}
  .eyebrow::before {{ content: ""; width: 18px; height: 1px; background: currentColor; }}
  h1 {{ max-width: 340px; margin: 0; font-size: clamp(30px, 9vw, 39px); letter-spacing: -.065em; line-height: 1.14; }}
  h1 em {{ color: var(--accent); font-style: normal; }}
  .intro {{ max-width: 320px; margin: 16px 0 0; color: var(--subtle); font-size: 15px; line-height: 1.7; }}
  .verify-panel {{ position: relative; padding: 19px 17px 17px; border: 1px solid var(--stroke); border-radius: 20px; background: var(--surface); box-shadow: 0 14px 34px color-mix(in srgb, var(--text) 7%, transparent); }}
  .panel-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 17px; }}
  .panel-title {{ display: flex; align-items: center; gap: 9px; font-size: 14px; font-weight: 730; }}
  .lock {{ width: 28px; height: 28px; display: grid; place-items: center; border-radius: 9px; color: var(--accent); background: var(--accent-faint); }}
  .lock svg {{ width: 15px; height: 15px; }}
  .panel-badge {{ padding: 4px 8px; border-radius: 99px; color: var(--success); background: color-mix(in srgb, var(--success) 11%, transparent); font-size: 11px; font-weight: 700; }}
  .journey {{ display: flex; align-items: center; margin: 2px 4px 19px; }}
  .journey-item {{ flex: 0 0 auto; display: flex; align-items: center; gap: 7px; color: var(--subtle); font-size: 11px; white-space: nowrap; }}
  .journey-dot {{ width: 8px; height: 8px; border: 2px solid var(--accent); border-radius: 50%; }}
  .journey-item.active {{ color: var(--text); font-weight: 700; }}
  .journey-item.active .journey-dot {{ border: 0; background: var(--accent); box-shadow: 0 0 0 4px var(--accent-faint); }}
  .journey-line {{ height: 1px; flex: 1; min-width: 8px; margin: 0 8px; background: var(--stroke); }}
  #turnstile-widget {{ min-height: 65px; display: grid; justify-content: center; align-items: center; margin: 0 0 14px; }}
  .status {{ min-height: 42px; display: flex; align-items: center; justify-content: center; gap: 8px; padding: 9px 11px; border-radius: 11px; color: var(--subtle); background: color-mix(in srgb, var(--bg) 64%, transparent); font-size: 12px; line-height: 1.4; text-align: center; }}
  .status::before {{ content: ""; width: 13px; height: 13px; flex: 0 0 auto; border: 2px solid color-mix(in srgb, var(--accent) 22%, transparent); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; }}
  .status.error {{ color: var(--danger); background: color-mix(in srgb, var(--danger) 10%, transparent); }}
  .status.error::before {{ content: "!"; display: grid; place-items: center; border: 0; color: #fff; background: var(--danger); animation: none; font-weight: 800; font-size: 10px; }}
  .status.ok {{ color: var(--success); background: color-mix(in srgb, var(--success) 11%, transparent); }}
  .status.ok::before {{ content: "✓"; display: grid; place-items: center; border: 0; color: #fff; background: var(--success); animation: none; font-weight: 800; font-size: 10px; }}
  .footer {{ display: flex; gap: 7px; align-items: center; margin: 17px 4px 0; color: var(--subtle); font-size: 11px; }}
  .footer svg {{ flex: 0 0 auto; width: 14px; height: 14px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  @media (max-height: 680px) {{ .hero {{ padding-top: 35px; padding-bottom: 22px; }} }}
</style>
</head><body><main>
  <header class="top"><span class="top-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="m12 3 2.2 4.8L19 10l-4.8 2.2L12 17l-2.2-4.8L5 10l4.8-2.2L12 3Z"/></svg></span>群组安全中心</header>
  <section class="hero"><div class="eyebrow">安全验证</div><h1>准备好加入<br><em>新的群组了吗？</em></h1><p class="intro">完成一次简短验证，我们会立即处理你的入群申请。</p></section>
  <section class="verify-panel">
    <div class="panel-head"><div class="panel-title"><span class="lock"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg></span>确认不是自动化账号</div><span class="panel-badge">安全连接</span></div>
    <div class="journey" aria-label="验证流程"><span class="journey-item active"><span class="journey-dot"></span>验证</span><span class="journey-line"></span><span class="journey-item"><span class="journey-dot"></span>批准</span><span class="journey-line"></span><span class="journey-item"><span class="journey-dot"></span>加入</span></div>
    <div id="turnstile-widget"></div>
    <div id="status" class="status" role="status">正在加载安全验证…</div>
  </section>
  <p class="footer"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3 5.5 6v5.5c0 4.3 2.8 7.7 6.5 9.5 3.7-1.8 6.5-5.2 6.5-9.5V6L12 3Z"/><path d="m9 12 2 2 4-4"/></svg>验证只用于防止滥用，不会公开你的个人信息</p>
</main>
<script>
const status = document.getElementById('status');
const session = new URLSearchParams(location.search).get('session');
const webApp = window.Telegram && window.Telegram.WebApp;
webApp && webApp.ready(); webApp && webApp.expand();
let sessionReady = false;
function show(message, kind) {{ status.textContent = message; status.className = `status ${{kind || ''}}`; }}
async function prepareSession() {{
  if (!session || !webApp || !webApp.initData) {{ show('请从 Telegram 的验证按钮打开此页面。', 'error'); return; }}
  show('正在确认验证会话…');
  try {{
    const response = await fetch('/join-verify/api/session-status', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{session_token: session, init_data: webApp.initData}})
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '验证会话无效');
    sessionReady = true;
    render();
  }} catch (error) {{ show(error.message || '验证会话无效，请重新打开最新验证按钮。', 'error'); }}
}}
async function complete(turnstileToken) {{
  if (!session || !webApp || !webApp.initData) {{ show('请从 Telegram 的验证按钮打开此页面。', 'error'); return; }}
  show('正在验证身份…');
  try {{
    const response = await fetch('/join-verify/api/complete', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{session_token: session, init_data: webApp.initData, turnstile_token: turnstileToken}})
    }});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '验证失败');
    show('验证通过，已自动批准入群。', 'ok');
    setTimeout(() => webApp.close(), 1400);
  }} catch (error) {{ show(error.message || '验证失败，请重试。', 'error'); }}
}}
function render() {{
  if (!sessionReady) return;
  if (!window.turnstile || typeof window.turnstile.render !== 'function') {{
    setTimeout(render, 100);
    return;
  }}
  try {{
    window.turnstile.render('#turnstile-widget', {{
      sitekey: '{escaped_site_key}',
      callback: complete,
      'error-callback': (code) => show(`验证服务初始化失败（${{code || '未知错误'}}）。请检查 Turnstile 域名配置后重试。`, 'error')
    }});
  }} catch (error) {{
    console.error('Turnstile render failed', error);
    const reason = error && error.message ? `（${{error.message}}）` : '';
    show(`验证服务初始化失败${{reason}}。请检查 Site Key、域名配置和小组件模式。`, 'error');
  }}
}}
prepareSession();
setTimeout(() => {{
  if (sessionReady && (!window.turnstile || typeof window.turnstile.render !== 'function')) {{
    show(window.turnstileScriptFailed
      ? '验证脚本加载失败。请检查当前网络能否访问 challenges.cloudflare.com。'
      : '无法加载验证服务。请检查当前网络能否访问 challenges.cloudflare.com。', 'error');
  }}
}}, 10000);
</script></body></html>"""
