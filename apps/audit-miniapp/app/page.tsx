'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  ArrowDownUp,
  Bot,
  Check,
  ChevronRight,
  Clock3,
  FileSearch,
  Filter,
  Flag,
  Link2,
  MessageSquareText,
  Search,
  ShieldAlert,
  ShieldCheck,
  UserRound,
  X,
} from 'lucide-react';
import { Button } from '@/components/ui/button';

type Status = '已封禁' | '待复核' | '已恢复' | '已放行' | '历史隔离';
type Risk = '高风险' | '中风险' | '低风险';
type AuditEvent = {
  id: string; time: string; user: string; username: string; status: Status; risk: Risk;
  category: string; confidence: number | null; isAdvertisement: boolean | null; ruleScore: number; message: string; normalized: string;
  evidence: string[]; source: string; action: string;
};

type ApiEvent = {
  id: number; chat_id: number; user_id: number; username?: string | null; raw_text: string;
  normalized_text: string; action: string; review_status: string; created_at: string;
  signals?: { score?: number; reasons?: string[] } | null;
  verdict?: { is_ad?: boolean; category?: string; confidence?: number; evidence?: string[] } | null;
};
type AuditPage = { items: ApiEvent[]; next_before_id: number | null };
type PageContext = { initData: string; chatId: string };
type AuditSummary = { permanent_bans: number; pending_review: number; quarantined: number; total: number };

const apiBase = '/audit-api';
const pageSize = 30;
const tabApiFilters: Record<string, string | undefined> = {
  '全部': undefined,
  '待复核': 'review',
  '已封禁': 'banned',
  '已恢复': 'restored',
};

function mapEvent(event: ApiEvent): AuditEvent {
  const confidence = typeof event.verdict?.confidence === 'number' ? event.verdict.confidence : null;
  const isAdvertisement = typeof event.verdict?.is_ad === 'boolean' ? event.verdict.is_ad : null;
  const ruleScore = event.signals?.score ?? 0;
  const status: Status = event.action === 'permanent_ban'
    ? '已封禁'
    : event.review_status === 'false_positive' || event.action === 'release'
      ? '已恢复'
      : event.review_status === 'pending' || event.action === 'needs_review'
        ? '待复核'
        : event.action === 'quarantine'
          ? '历史隔离'
          : '已放行';
  const riskScore = isAdvertisement ? (confidence ?? 0) : confidence === null ? Math.min(1, ruleScore / 8) : 0;
  return {
    id: `AUD-${event.id}`,
    time: new Date(event.created_at).toLocaleString('zh-CN', { hour12: false }),
    user: `用户 ${event.user_id}`,
    username: event.username ? `@${event.username.replace(/^@/, '')}` : '—',
    status,
    risk: riskScore >= 0.9 ? '高风险' : riskScore >= 0.6 ? '中风险' : '低风险',
    category: event.verdict?.category || '规则初筛',
    confidence,
    isAdvertisement,
    ruleScore,
    message: event.raw_text || '（媒体消息）',
    normalized: event.normalized_text || '—',
    evidence: [...(event.signals?.reasons || []), ...(event.verdict?.evidence || [])],
    source: event.verdict ? '大模型裁决' : '规则初筛',
    action: status === '已封禁' ? '删除消息 · 永久封禁' : status === '待复核' ? '等待管理员复核 · 消息保持可见' : status === '已恢复' ? '已恢复发言权限' : status === '历史隔离' ? '历史策略：消息已删除并临时禁言' : '已放行 · 未执行 Telegram 处置',
  };
}

const tabs: Array<{ label: string; statuses?: Status[] }> = [
  { label: '全部' }, { label: '待复核', statuses: ['待复核', '历史隔离'] },
  { label: '已封禁', statuses: ['已封禁'] }, { label: '已恢复', statuses: ['已恢复'] },
];
const statusStyle: Record<Status, string> = {
  已封禁: 'bg-rose-500/12 text-rose-700 ring-rose-500/20', 待复核: 'bg-amber-400/15 text-amber-800 ring-amber-500/25',
  已恢复: 'bg-emerald-500/12 text-emerald-700 ring-emerald-500/20', 已放行: 'bg-emerald-500/12 text-emerald-700 ring-emerald-500/20', 历史隔离: 'bg-sky-500/12 text-sky-700 ring-sky-500/20',
};

export default function Home() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [tab, setTab] = useState('全部');
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const [groupTitle, setGroupTitle] = useState('');
  const [pageContext, setPageContext] = useState<PageContext | null>(null);
  const [summary, setSummary] = useState<AuditSummary | null>(null);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const loadMoreRef = useRef<HTMLDivElement>(null);

  async function fetchAuditPage(
    context: PageContext, beforeId?: number, filter?: string,
  ): Promise<AuditPage> {
    const parameters = new URLSearchParams({ limit: String(pageSize), chat_id: context.chatId });
    if (beforeId) parameters.set('before_id', String(beforeId));
    if (filter) parameters.set('filter', filter);
    const response = await fetch(`${apiBase}/v1/audit/events?${parameters}`, {
      headers: { 'X-Telegram-Init-Data': context.initData },
    });
    if (!response.ok) throw new Error((await response.json()).detail || '无权读取审计日志');
    const payload = await response.json() as AuditPage | ApiEvent[];
    // Allow a rolling deployment: the previous API returned a bare array.
    // It remains readable, but does not advertise a next cursor page.
    if (Array.isArray(payload)) return { items: payload, next_before_id: null };
    if (!Array.isArray(payload.items)) throw new Error('审计服务返回了无效数据');
    return payload;
  }

  async function fetchAuditSummary(context: PageContext): Promise<AuditSummary> {
    const parameters = new URLSearchParams({ chat_id: context.chatId });
    const response = await fetch(`${apiBase}/v1/audit/summary?${parameters}`, {
      headers: { 'X-Telegram-Init-Data': context.initData },
    });
    if (!response.ok) throw new Error((await response.json()).detail || '无法读取审计汇总');
    return response.json() as Promise<AuditSummary>;
  }

  useEffect(() => {
    const telegram = (window as Window & { Telegram?: { WebApp?: { ready: () => void; expand: () => void; initData?: string } } }).Telegram;
    telegram?.WebApp?.ready(); telegram?.WebApp?.expand();
    const initData = telegram?.WebApp?.initData;
    const chatId = new URLSearchParams(window.location.search).get('chat_id');
    const chatTitle = new URLSearchParams(window.location.search).get('chat_title');
    if (!initData) {
      setNotice('请从 Bot 的审计按钮打开此页面。');
      return;
    }
    if (!chatId) {
      setNotice('审计链接缺少群组信息，请在群内重新使用 /audit。');
      return;
    }
    const context = { initData, chatId };
    setGroupTitle(chatTitle?.trim() || `群组 ${chatId}`);
    setPageContext(context);
  }, []);

  useEffect(() => {
    if (!pageContext) return;
    let cancelled = false;
    setHasMore(false);
    setNextBeforeId(null);
    fetchAuditPage(pageContext, undefined, tabApiFilters[tab])
      .then((page) => {
        if (cancelled) return;
        const mapped = page.items.map(mapEvent);
        setEvents(mapped);
        setSelectedId(mapped[0]?.id || '');
        setNextBeforeId(page.next_before_id);
        setHasMore(page.next_before_id !== null);
      })
      .catch((error: Error) => { if (!cancelled) setNotice(error.message || '加载审计日志失败。'); });
    return () => { cancelled = true; };
  }, [pageContext, tab]);

  useEffect(() => {
    if (!pageContext) return;
    let cancelled = false;
    fetchAuditSummary(pageContext)
      .then((value) => { if (!cancelled) setSummary(value); })
      .catch((error: Error) => { if (!cancelled) setNotice(error.message || '加载审计汇总失败。'); });
    return () => { cancelled = true; };
  }, [pageContext]);

  async function loadMore() {
    if (!pageContext || !hasMore || !nextBeforeId || isLoadingMore) return;
    setIsLoadingMore(true);
    try {
      const page = await fetchAuditPage(pageContext, nextBeforeId, tabApiFilters[tab]);
      setEvents((current) => [...current, ...page.items.map(mapEvent)]);
      setNextBeforeId(page.next_before_id);
      setHasMore(page.next_before_id !== null);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '加载更多审计记录失败。');
    } finally {
      setIsLoadingMore(false);
    }
  }

  useEffect(() => {
    const target = loadMoreRef.current;
    if (!target || !hasMore || isLoadingMore || !pageContext || !nextBeforeId) return;
    const observer = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) void loadMore(); },
      { rootMargin: '360px' },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, isLoadingMore, nextBeforeId, pageContext, tab]);

  const activeTab = tabs.find((item) => item.label === tab);
  const visible = useMemo(() => events.filter((item) => {
    const matchedStatus = !activeTab?.statuses || activeTab.statuses.includes(item.status);
    const searchable = `${item.user} ${item.username} ${item.category} ${item.message}`.toLowerCase();
    return matchedStatus && (!query.trim() || searchable.includes(query.trim().toLowerCase()));
  }), [events, activeTab?.statuses, query]);
  const selected = events.find((item) => item.id === selectedId) ?? visible[0];
  function selectEvent(eventId: string) {
    setSelectedId(eventId);
    setMobileDetailOpen(true);
  }
  async function updateSelected(status: Status, action: string) {
    if (!selected) return;
    const telegram = (window as Window & { Telegram?: { WebApp?: { initData?: string } } }).Telegram;
    const initData = telegram?.WebApp?.initData;
    if (!initData) { setNotice('身份验证已失效，请重新打开审计页。'); return; }
    const chatId = new URLSearchParams(window.location.search).get('chat_id');
    if (!chatId) { setNotice('审计链接缺少群组信息，请在群内重新使用 /audit。'); return; }
    const eventId = selected.id.replace('AUD-', '');
    const endpoint = status === '已恢复' ? 'restore' : 'confirm-ban';
    try {
      const response = await fetch(`${apiBase}/v1/audit/events/${eventId}/${endpoint}?chat_id=${encodeURIComponent(chatId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData },
        body: JSON.stringify({ note: '由审计 Mini App 操作' }),
      });
      if (!response.ok) throw new Error((await response.json()).detail || '操作失败');
      setEvents((current) => current.map((item) => item.id === selected.id ? { ...item, status, action } : item));
      if (pageContext) fetchAuditSummary(pageContext).then(setSummary).catch(() => undefined);
      setNotice(status === '已恢复' ? '已标记为误封。' : '已确认封禁并写入审计记录。');
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '操作失败。');
    }
  }

  return <main className="min-h-dvh bg-[#f2f6f8] text-[#14222d]">
    <div className="mx-auto max-w-6xl px-4 pb-10 pt-5 sm:px-6 sm:pt-8">
      <header className="mb-6 flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3"><div className="grid size-11 shrink-0 place-items-center rounded-2xl bg-[#172f43] text-[#b7f4e6] shadow-lg shadow-[#17334c]/15"><ShieldCheck className="size-5" strokeWidth={2.4} /></div><div className="min-w-0"><p className="text-xs font-semibold tracking-[0.16em] text-[#627988]">MISAKA GUARD</p><h1 className="truncate text-lg font-bold tracking-tight">群组审计中心</h1></div></div>
        <div className="flex items-center gap-2 rounded-full border border-[#d6e0e4] bg-white px-3 py-2 text-xs font-medium text-[#456070] shadow-sm"><span className="size-2 rounded-full bg-emerald-500 shadow-[0_0_0_3px_rgba(34,197,94,.12)]" />实时连接</div>
      </header>
      <section className="mb-5 overflow-hidden rounded-[1.4rem] bg-[#172f43] px-5 py-5 text-white shadow-xl shadow-[#17334c]/10 sm:px-7"><div className="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between"><div><div className="mb-2 flex items-center gap-2 text-sm text-[#add4d5]"><MessageSquareText className="size-4" /><span className="truncate">{groupTitle || '当前群组审计范围'}</span></div><p className="text-2xl font-bold tracking-tight">审计记录总计 {summary?.total ?? '—'} 条</p><p className="mt-1 text-sm text-[#b5cbd5]">当前筛选已加载 {events.length} 条 · 审计记录仅对已授权的群管理员开放</p></div><div className="flex gap-5 text-sm"><div><p className="text-2xl font-bold text-[#b7f4e6]">{summary?.permanent_bans ?? '—'}</p><p className="text-[#b5cbd5]">永久封禁</p></div><div><p className="text-2xl font-bold text-white">{summary ? summary.pending_review + summary.quarantined : '—'}</p><p className="text-[#b5cbd5]">等待处置</p></div></div></div></section>
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <section className="min-w-0 rounded-[1.35rem] border border-[#dce6e9] bg-white shadow-sm"><div className="border-b border-[#e4ebed] px-4 pt-4 sm:px-5 sm:pt-5"><div className="flex items-center justify-between gap-3"><div><h2 className="font-bold">审计事件</h2><p className="mt-0.5 text-xs text-[#6d8491]">可查看原始内容、判定依据与处置结果</p></div><Button variant="outline" size="sm" className="border-[#d6e0e4] text-[#355464]"><ArrowDownUp />最新优先</Button></div><div className="mt-4 flex gap-1 overflow-x-auto pb-0.5">{tabs.map((item) => <button key={item.label} onClick={() => setTab(item.label)} className={`shrink-0 border-b-2 px-3 py-2 text-sm font-semibold transition ${tab === item.label ? 'border-[#1a9689] text-[#08776e]' : 'border-transparent text-[#6a828f] hover:text-[#294858]'}`}>{item.label}</button>)}</div></div>
          <div className="flex items-center gap-2 border-b border-[#e4ebed] px-4 py-3 sm:px-5"><div className="flex min-w-0 flex-1 items-center gap-2 rounded-xl bg-[#f2f6f8] px-3 py-2 text-[#6c8390]"><Search className="size-4 shrink-0" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索用户、内容或类型" className="min-w-0 flex-1 bg-transparent text-sm text-[#1c3542] outline-none placeholder:text-[#8ba0aa]" /></div><Button variant="outline" size="icon" className="shrink-0 border-[#d6e0e4] text-[#456070]" aria-label="筛选"><Filter /></Button></div>
          <div className="divide-y divide-[#e8eef0]">{visible.map((item) => <button key={item.id} onClick={() => selectEvent(item.id)} className={`flex w-full items-start gap-3 px-4 py-4 text-left transition hover:bg-[#f7fbfb] sm:px-5 ${selected?.id === item.id ? 'bg-[#ecf9f6]' : ''}`}><div className={`mt-0.5 grid size-9 shrink-0 place-items-center rounded-xl ${item.risk === '高风险' ? 'bg-rose-500/10 text-rose-600' : 'bg-amber-400/15 text-amber-700'}`}>{item.risk === '高风险' ? <ShieldAlert className="size-4" /> : <Flag className="size-4" />}</div><div className="min-w-0 flex-1"><div className="flex items-center justify-between gap-3"><p className="truncate text-sm font-bold">{item.user}</p><time className="shrink-0 text-xs text-[#78909b]">{item.time}</time></div><p className="mt-0.5 truncate text-xs text-[#69818e]">{item.category} · {item.username}</p><p className="mt-2 line-clamp-2 text-sm leading-5 text-[#385361]">{item.message}</p><div className="mt-2 flex items-center gap-2"><span className={`rounded-full px-2 py-0.5 text-[11px] font-bold ring-1 ring-inset ${statusStyle[item.status]}`}>{item.status}</span><span className={item.isAdvertisement === true ? 'text-xs font-semibold text-rose-600' : item.confidence !== null ? 'text-xs font-semibold text-emerald-700' : 'text-xs font-semibold text-sky-700'}>{item.confidence !== null ? `${item.isAdvertisement ? '广告' : '正常'} ${Math.round(item.confidence * 100)}%` : `规则 ${item.ruleScore} 分`}</span></div></div><ChevronRight className="mt-2 size-4 shrink-0 text-[#9db0b8]" /></button>)}{visible.length === 0 && <div className="grid place-items-center gap-2 px-5 py-14 text-center text-[#718993]"><FileSearch className="size-7" /><p className="text-sm">没有符合条件的审计事件</p></div>}{hasMore && <div ref={loadMoreRef} className="px-5 py-5 text-center text-xs font-medium text-[#6b8490]">{isLoadingMore ? '正在加载更多记录…' : '继续下滑以加载更多记录'}</div>}</div>
        </section>
        <aside className="hidden rounded-[1.35rem] border border-[#dce6e9] bg-white p-5 shadow-sm lg:sticky lg:top-5 lg:block lg:h-fit">{selected && <EventDetail event={selected} onAction={updateSelected} />}</aside>
      </div>
    </div>
    {mobileDetailOpen && selected && <div className="fixed inset-0 z-40 bg-[#102530]/45 p-3 backdrop-blur-[2px] lg:hidden" role="dialog" aria-modal="true" aria-label="审计事件详情">
      <div className="absolute inset-x-0 bottom-0 max-h-[88dvh] overflow-y-auto rounded-t-[1.7rem] bg-white p-5 shadow-2xl">
        <div className="mx-auto mb-4 h-1.5 w-11 rounded-full bg-[#d4e0e4]" />
        <button onClick={() => setMobileDetailOpen(false)} className="absolute right-4 top-4 grid size-9 place-items-center rounded-full bg-[#eef4f5] text-[#456070]" aria-label="关闭详情"><X className="size-5" /></button>
        <EventDetail event={selected} onAction={updateSelected} />
      </div>
    </div>}
    {notice && <div role="status" className="fixed inset-x-4 bottom-5 z-50 mx-auto w-fit rounded-full bg-[#173e4c] px-4 py-2.5 text-sm font-medium text-white shadow-xl">{notice}</div>}
  </main>;
}

function EventDetail({ event, onAction }: { event: AuditEvent; onAction: (status: Status, action: string) => void }) {
  const isReviewable = event.status === '待复核' || event.status === '历史隔离';
  return <div><div className="flex items-start justify-between gap-3"><div><p className="text-xs font-semibold tracking-[0.13em] text-[#6d8793]">事件详情</p><h2 className="mt-1 text-lg font-bold">{event.id}</h2></div><span className={`rounded-full px-2.5 py-1 text-xs font-bold ring-1 ring-inset ${statusStyle[event.status]}`}>{event.status}</span></div>
    <div className="mt-5 space-y-4 text-sm"><DetailRow icon={<UserRound />} label="发送者" value={`${event.user} ${event.username}`} /><DetailRow icon={<Clock3 />} label="发生时间" value={`今天 ${event.time}`} /><DetailRow icon={<Bot />} label="判定来源" value={event.source} /><DetailRow icon={<Activity />} label={event.confidence !== null ? (event.isAdvertisement ? '广告置信度' : '非广告判断置信度') : '规则风险分'} value={event.confidence !== null ? `${Math.round(event.confidence * 100)}% · ${event.category}` : `${event.ruleScore} 分 · 未经 Kimi 判定`} /></div>
    <div className="mt-5 rounded-xl border border-[#e0e9eb] bg-[#f6f9fa] p-3.5"><p className="mb-2 text-xs font-semibold text-[#66808c]">原始消息</p><p className="text-sm leading-6 text-[#264351]">{event.message}</p><p className="mt-3 border-t border-[#e0e9eb] pt-3 text-xs leading-5 text-[#718995]">归一化：{event.normalized}</p></div>
    <div className="mt-5"><p className="mb-2 text-xs font-semibold text-[#66808c]">判定证据</p><div className="flex flex-wrap gap-2">{event.evidence.map((item) => <span key={item} className="rounded-lg bg-[#e9f5f3] px-2.5 py-1.5 text-xs font-semibold text-[#17776e]">{item}</span>)}</div></div>
    <div className="mt-5 border-t border-[#e3ebed] pt-4"><p className="mb-3 text-xs text-[#728b96]">当前动作：{event.action}</p>{isReviewable ? <div className="grid grid-cols-2 gap-2"><Button variant="outline" onClick={() => onAction('已恢复', '解除限制 · 标记为误封')} className="border-[#b9d3d4] text-[#23626a]"><X />误封恢复</Button><Button onClick={() => onAction('已封禁', '删除消息 · 永久封禁')} className="bg-[#c94656] text-white hover:bg-[#b83e4d]"><Check />确认封禁</Button></div> : <Button variant="outline" className="w-full border-[#d6e0e4] text-[#456070]"><Link2 />复制事件编号</Button>}</div>
  </div>;
}

function DetailRow({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return <div className="flex gap-3"><span className="mt-0.5 text-[#4e9f9a]">{icon}</span><div><p className="text-xs text-[#718995]">{label}</p><p className="mt-0.5 font-medium text-[#2b4856]">{value}</p></div></div>;
}
