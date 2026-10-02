import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Search, Loader2, Bot, Share2, ArrowRight, Layers, UsersRound, Zap, Database,
  ShieldCheck, ShieldAlert,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import AssignmentPanel from '../../components/common/AssignmentPanel';
import QbiGrantPanel from '../../components/common/QbiGrantPanel';
import UserDistribution from './UserDistribution';
import { useNavigate, useSearchParams } from 'react-router-dom';

interface ResMeta {
  /** 最新 published 版本号(15 号无默认,列表仅展示「已发布 N 版」徽标) */
  publishedVersion: number | null;
  assignedCount: number;
  /** 完整版本/指派数据,供右侧指派面板挂载时复用,避免重复请求 */
  versions: api.ResourceVersion[];
  assignments: api.ResourceAssignment[];
}

type View = 'agent' | 'skill' | 'quickbi' | 'user';

const KIND_TEXT: Record<api.AssignableType, { label: string; errCode: string; detailPath: string }> = {
  agent: { label: 'Agent', errCode: 'agent_not_distributed', detailPath: '/tools/agent/detail' },
  skill: { label: 'Skill', errCode: 'skill_not_distributed', detailPath: '/tools/skill/detail' },
};

/** 按资源(Agent / Skill)管理分发:左列表 + 右指派面板。两类资源交互完全一致,
 *  差别只在打哪套接口和文案 —— 所以做成同一个组件,而不是复制一份改名字。 */
const ResourceDistribution: React.FC<{ kind: api.AssignableType }> = ({ kind }) => {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const urlId = params.get('id');
  const [items, setItems] = useState<any[]>([]);
  const [meta, setMeta] = useState<Record<string, ResMeta>>({});
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [selected, setSelected] = useState<string | null>(urlId);
  const text = KIND_TEXT[kind];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { items: list } = kind === 'skill' ? await api.listSkills() : await api.listAgents();
      setItems(list);

      // 并行加载每个资源的版本与指派,统计已发布版本与已指派用户数
      const results = await Promise.allSettled(list.map(async (a: any) => {
        const [v, asg] = await Promise.all([
          api.listResourceVersions(kind, a.id),
          api.listAssignments(a.id, kind),
        ]);
        const published = (v.items || []).filter((x) => x.status === 'published');
        return {
          id: a.id,
          publishedVersion: published.length ? Math.max(...published.map((x) => x.version ?? 0)) : null,
          assignedCount: (asg.items || []).length,
          versions: v.items,
          assignments: asg.items || [],
        };
      }));
      const m: Record<string, ResMeta> = {};
      list.forEach((a: any, i: number) => {
        const r = results[i];
        m[a.id] = r.status === 'fulfilled'
          ? {
              publishedVersion: r.value.publishedVersion,
              assignedCount: r.value.assignedCount,
              versions: r.value.versions,
              assignments: r.value.assignments,
            }
          : { publishedVersion: null, assignedCount: 0, versions: [], assignments: [] };
      });
      setMeta(m);

      const target = urlId && list.some((a: any) => a.id === urlId) ? urlId : (list[0]?.id ?? null);
      setSelected(target);
      if (target !== urlId && params.has('id')) {
        setParams({}, { replace: true });
      }
    } catch (e: any) {
      toast.error(e.message || `加载 ${text.label} 列表失败`);
    } finally {
      setLoading(false);
    }
    // params/setParams 只在首次对齐 URL 用,不纳入依赖以免每次 setParams 触发重载
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, urlId]);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(
    () => items.filter((a: any) => !keyword || (a.name || '').includes(keyword) || a.id.includes(keyword)),
    [items, keyword],
  );

  const current = items.find((a: any) => a.id === selected) || null;
  const currentMeta = current ? meta[current.id] : null;

  return (
    <div className="grid lg:grid-cols-[340px_1fr] gap-6 items-start min-w-0">
      {/* ── 左:资源列表 ── */}
      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/60 backdrop-blur-sm">
        <CardHeader className="border-b bg-slate-50/50 py-3 px-5">
          <CardTitle className="text-xs font-black uppercase tracking-widest text-slate-500 flex items-center gap-2">
            {kind === 'skill'
              ? <Zap size={14} className="text-amber-500" />
              : <Bot size={14} className="text-blue-600" />} {text.label} 列表
          </CardTitle>
        </CardHeader>
        <CardContent className="p-4 space-y-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
            <Input value={keyword} onChange={(e) => setKeyword(e.target.value)}
              placeholder={`搜索名称 / ${kind}_id...`} className="pl-9 h-9 bg-white border-slate-200 rounded-xl text-sm" />
          </div>

          {loading ? (
            <div className="flex justify-center py-14 text-slate-300"><Loader2 size={22} className="animate-spin" /></div>
          ) : filtered.length === 0 ? (
            <div className="text-center py-14 text-slate-300 text-xs">暂无 {text.label}</div>
          ) : (
            <div className="space-y-2 max-h-[62vh] overflow-y-auto pr-1 custom-scrollbar">
              {filtered.map((a: any) => {
                const m = meta[a.id];
                const active = selected === a.id;
                const publishedCount = a.published_count ?? m?.versions?.filter((x: any) => x.status === 'published').length ?? 0;
                return (
                  <button key={a.id} onClick={() => setSelected(a.id)}
                    className={cn(
                      'w-full text-left rounded-2xl border p-3.5 transition-all group',
                      active
                        ? 'border-blue-500 bg-blue-50/60 shadow-md shadow-blue-100'
                        : 'border-slate-100 bg-white hover:border-blue-200 hover:bg-slate-50/50',
                    )}>
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="text-sm font-black text-slate-800 truncate">{a.name}</div>
                        <div className="text-[10px] text-slate-400 font-mono mt-0.5 truncate">{a.id}</div>
                      </div>
                      <Badge variant="outline" className={cn(
                        'rounded-full text-[9px] font-black tracking-wider shrink-0',
                        publishedCount > 0
                          ? 'bg-emerald-50 text-emerald-600 border-emerald-200'
                          : 'bg-amber-50 text-amber-600 border-amber-200',
                      )}>
                        {publishedCount > 0 ? `已发布 ${publishedCount} 版` : '未发布'}
                      </Badge>
                    </div>
                    <div className="flex items-center gap-3 mt-3 pt-3 border-t border-slate-50">
                      <div className="flex-1">
                        <div className="text-[9px] font-bold text-slate-400 uppercase tracking-tighter">最新已发布</div>
                        <div className="text-xs font-black text-slate-700 mt-0.5">
                          {m?.publishedVersion != null ? `v${m.publishedVersion}` : <span className="text-slate-300 font-medium">无(发布≠生效)</span>}
                        </div>
                      </div>
                      <div className="flex-1">
                        <div className="text-[9px] font-bold text-slate-400 uppercase tracking-tighter">已指派</div>
                        <div className="text-xs font-black text-slate-700 mt-0.5">
                          {m?.assignedCount ?? a.assigned_count ?? 0} <span className="text-slate-300 font-medium">位用户</span>
                        </div>
                      </div>
                      <ArrowRight size={14} className={cn('shrink-0 transition-colors', active ? 'text-blue-500' : 'text-slate-200 group-hover:text-blue-300')} />
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── 右:选中资源的分发管理 ── */}
      {/* min-w-0:允许内容收缩,表格/长文本不会把 grid 撑宽导致整页横向滚动 */}
      <div className="space-y-4 min-w-0">
        {!current ? (
          <Card className="border-slate-200 shadow-sm">
            <CardContent className="p-14 text-center text-slate-300 text-sm">
              <Layers className="mx-auto mb-3 text-slate-200" size={32} />
              在左侧选择要管理的 {text.label}
            </CardContent>
          </Card>
        ) : (
          <>
            <Card className="border-slate-200 shadow-sm overflow-hidden">
              <CardContent className="p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div className="flex items-center gap-4 min-w-0">
                  <div className={cn('h-12 w-12 rounded-2xl text-white flex items-center justify-center font-black text-lg shrink-0 shadow-lg',
                    kind === 'skill' ? 'bg-amber-500 shadow-amber-200' : 'bg-blue-600 shadow-blue-200')}>
                    {kind === 'skill' ? <Zap size={20} /> : current.name[0]}
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <h2 className="text-lg font-black text-slate-900">{current.name}</h2>
                      {kind === 'agent' && !current.active && (
                        <Badge variant="outline" className="rounded-full text-[9px] font-black text-slate-400 border-slate-200">OFFLINE（离线）</Badge>
                      )}
                    </div>
                    <div className="text-[11px] text-slate-400 font-mono">{current.id}</div>
                    <div className="text-[11px] text-slate-500 mt-1 truncate"
                      title={(kind === 'skill' ? current.description : current.persona_desc) || '未填写描述'}>
                      {(kind === 'skill' ? current.description : current.persona_desc) || '未填写描述'}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-6 shrink-0">
                  <div className="text-center">
                    <div className="text-[9px] font-bold text-slate-400 uppercase tracking-tighter">最新已发布</div>
                    <div className="text-xl font-black text-slate-900 mt-0.5">{currentMeta?.publishedVersion != null ? `v${currentMeta.publishedVersion}` : '—'}</div>
                  </div>
                  <div className="text-center">
                    <div className="text-[9px] font-bold text-slate-400 uppercase tracking-tighter">已指派用户</div>
                    <div className="text-xl font-black text-slate-900 mt-0.5">{currentMeta?.assignedCount ?? 0}</div>
                  </div>
                  <Button variant="outline" size="sm" className="h-9 rounded-xl text-xs font-bold text-blue-600 border-blue-200 hover:bg-blue-50 gap-1.5"
                    onClick={() => navigate(`${text.detailPath}?id=${current.id}`, { state: { from: 'distribution' } })}>
                    查看详情 <ArrowRight size={13} />
                  </Button>
                </div>
              </CardContent>
            </Card>

            <AssignmentPanel key={`${kind}:${current.id}`} agentId={current.id} resourceType={kind}
              onChanged={() => load()}
              initialAssignments={currentMeta?.assignments}
              initialVersions={currentMeta?.versions} />
          </>
        )}
      </div>
    </div>
  );
};

/** 按 Quick BI 连接管理分配(0075)。
 *  Quick BI 是个特例:它没有版本,分的是「能不能用这套凭证」,所以右侧挂的是
 *  分配面板而不是版本指派面板;左侧徽标显示的也是「白名单 N 人 / 未启用」。 */
const QbiDistribution: React.FC = () => {
  const [items, setItems] = useState<api.QbiConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { items: list } = await api.qbiListConnections();
      // 环境变量连接(virtual)不落库,没有分配一说,不进这个列表
      const real = (list || []).filter((c) => !c.virtual);
      setItems(real);
      setSelected((prev) => (prev && real.some((c) => c.id === prev) ? prev : real[0]?.id ?? null));
    } catch (e: any) {
      toast.error(e.message || '加载 Quick BI 连接失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(
    () => items.filter((c) => !keyword || (c.name || '').includes(keyword) || c.id.includes(keyword)),
    [items, keyword],
  );
  const current = items.find((c) => c.id === selected) || null;

  return (
    <div className="grid lg:grid-cols-[340px_1fr] gap-6 items-start min-w-0">
      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/60 backdrop-blur-sm">
        <CardHeader className="border-b bg-slate-50/50 py-3 px-5">
          <CardTitle className="text-xs font-black uppercase tracking-widest text-slate-500 flex items-center gap-2">
            <Database size={14} className="text-cyan-600" /> Quick BI 连接
          </CardTitle>
        </CardHeader>
        <CardContent className="p-4 space-y-3">
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
            <Input value={keyword} onChange={(e) => setKeyword(e.target.value)}
              placeholder="搜索名称 / connection_id..." className="pl-9 h-9 bg-white border-slate-200 rounded-xl text-sm" />
          </div>
          {loading ? (
            <div className="flex justify-center py-14 text-slate-300"><Loader2 size={22} className="animate-spin" /></div>
          ) : filtered.length === 0 ? (
            <div className="text-center py-14 text-slate-300 text-xs">暂无 Quick BI 连接</div>
          ) : (
            <div className="space-y-2 max-h-[62vh] overflow-y-auto pr-1 custom-scrollbar">
              {filtered.map((c) => {
                const active = selected === c.id;
                const restricted = !!c.restricted;
                return (
                  <button key={c.id} onClick={() => setSelected(c.id)}
                    className={cn('w-full text-left rounded-2xl border p-3.5 transition-all group',
                      active ? 'border-blue-500 bg-blue-50/60 shadow-md shadow-blue-100'
                        : 'border-slate-100 bg-white hover:border-blue-200 hover:bg-slate-50/50')}>
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="text-sm font-black text-slate-800 truncate">
                          {c.name}
                          {c.is_default && <span className="ml-1.5 text-[9px] font-bold text-blue-500">默认</span>}
                        </div>
                        <div className="text-[10px] text-slate-400 font-mono mt-0.5 truncate">{c.id}</div>
                      </div>
                      <Badge variant="outline" className={cn(
                        'rounded-full text-[9px] font-black tracking-wider shrink-0',
                        restricted ? 'bg-emerald-50 text-emerald-600 border-emerald-200'
                          : 'bg-slate-100 text-slate-500 border-slate-200')}>
                        {restricted
                          ? <><ShieldCheck size={9} className="inline mr-0.5" />白名单 {c.granted_count}</>
                          : <><ShieldAlert size={9} className="inline mr-0.5" />未启用</>}
                      </Badge>
                    </div>
                    <div className="flex items-center gap-3 mt-3 pt-3 border-t border-slate-50">
                      <div className="flex-1">
                        <div className="text-[9px] font-bold text-slate-400 uppercase tracking-tighter">可见范围</div>
                        <div className="text-xs font-black text-slate-700 mt-0.5">
                          {({ private: '仅创建者', scenic: '本景区', group: '集团内', public: '全部可见' } as any)[c.visibility] || c.visibility}
                        </div>
                      </div>
                      <div className="flex-1">
                        <div className="text-[9px] font-bold text-slate-400 uppercase tracking-tighter">已分配</div>
                        <div className="text-xs font-black text-slate-700 mt-0.5">
                          {c.granted_count ?? 0} <span className="text-slate-300 font-medium">位用户</span>
                        </div>
                      </div>
                      <ArrowRight size={14} className={cn('shrink-0 transition-colors', active ? 'text-blue-500' : 'text-slate-200 group-hover:text-blue-300')} />
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <div className="space-y-4 min-w-0">
        {!current ? (
          <Card className="border-slate-200 shadow-sm">
            <CardContent className="p-14 text-center text-slate-300 text-sm">
              <Layers className="mx-auto mb-3 text-slate-200" size={32} />
              在左侧选择要分配的 Quick BI 连接
            </CardContent>
          </Card>
        ) : (
          <>
            <Card className="border-slate-200 shadow-sm overflow-hidden">
              <CardContent className="p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div className="flex items-center gap-4 min-w-0">
                  <div className="h-12 w-12 rounded-2xl bg-cyan-600 text-white flex items-center justify-center shrink-0 shadow-lg shadow-cyan-200">
                    <Database size={20} />
                  </div>
                  <div className="min-w-0">
                    <h2 className="text-lg font-black text-slate-900">{current.name}</h2>
                    <div className="text-[11px] text-slate-400 font-mono">{current.id}</div>
                    <div className="text-[11px] text-slate-500 mt-1 truncate">{current.server_domain}</div>
                  </div>
                </div>
                <div className="flex items-center gap-6 shrink-0">
                  <div className="text-center">
                    <div className="text-[9px] font-bold text-slate-400 uppercase tracking-tighter">分配模式</div>
                    <div className="text-sm font-black text-slate-900 mt-0.5">
                      {current.restricted ? '白名单' : '按景区/可见性'}
                    </div>
                  </div>
                  <div className="text-center">
                    <div className="text-[9px] font-bold text-slate-400 uppercase tracking-tighter">已分配用户</div>
                    <div className="text-xl font-black text-slate-900 mt-0.5">{current.granted_count ?? 0}</div>
                  </div>
                </div>
              </CardContent>
            </Card>

            <QbiGrantPanel key={current.id} connectionId={current.id} connectionName={current.name}
              onChanged={() => load()} />
          </>
        )}
      </div>
    </div>
  );
};

/** 分发中心(15 号 + 0074 + 0075):按 Agent / Skill / Quick BI / 用户四视角管理"谁能用什么"。
 *  无默认版本概念:发布 ≠ 生效,只有「按用户指派」才让某用户可用(fail-closed,超管豁免)。 */
const AgentDistribution: React.FC = () => {
  const [params, setParams] = useSearchParams();
  const [view, setView] = useState<View>(() => {
    const v = params.get('view');
    return v === 'user' || v === 'skill' || v === 'quickbi' ? (v as View) : 'agent';
  });

  const switchView = (v: View) => {
    setView(v);
    const next = new URLSearchParams(params);
    if (v === 'agent') next.delete('view'); else next.set('view', v);
    next.delete('id');   // 换视角时 id 不再有意义(agent id ≠ skill id)
    setParams(next, { replace: true });
  };

  const TABS: { key: View; label: string; icon: React.ReactNode }[] = [
    { key: 'agent', label: '按 Agent 管理', icon: <Bot size={13} /> },
    { key: 'skill', label: '按 Skill 管理', icon: <Zap size={13} /> },
    { key: 'quickbi', label: '按 Quick BI 管理', icon: <Database size={13} /> },
    { key: 'user', label: '按用户管理', icon: <UsersRound size={13} /> },
  ];

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      {/* 窄屏布局:说明文字是一大段,而 Tab 一行四个 —— 并排时如果允许 Tab 被压缩,
          标签会逐字换行成「按/Agent/管理」竖排,和标题挤在一起(现场截图就是这样)。
          三条一起才治得住:文字块 min-w-0 允许自己收缩、Tab 组 shrink-0 不许被压、
          按钮 whitespace-nowrap 不许拆词;真的放不下时整行横向滚动,而不是变形。
          断点用 xl 而不是 md:md(768px)时并排仍然太挤,那正是出问题的那一档。 */}
      <div className="flex flex-col xl:flex-row xl:items-start justify-between gap-4">
        <div className="min-w-0 xl:max-w-3xl">
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight flex items-center gap-2.5">
            <Share2 className="text-blue-600" size={24} /> 分发中心
          </h1>
          <p className="text-slate-500 text-sm mt-1">
            分发粒度是<b>人</b>（无默认版本、无景区范围）。发布 ≠ 生效，需按用户指派；
            未指派用户调用将报 agent_not_distributed / skill_not_distributed（fail-closed，超级管理员不受限）。
            Skill 指派后，APP 可直接按 skill_id 调用该技能。Quick BI 是特例：它没有版本，
            分的是「能不能用这套连接」——一个连接一旦被分配过，就只对被分配的人开放。
          </p>
        </div>
        <div className="-mx-1 shrink-0 overflow-x-auto px-1 pb-1 xl:pb-0">
          <div className="inline-flex rounded-xl border border-slate-200 bg-white p-1 shadow-sm">
            {TABS.map((t) => (
              <button key={t.key} onClick={() => switchView(t.key)}
                className={cn('flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg',
                  'px-3.5 py-2 text-xs font-black transition-all',
                  view === t.key ? 'bg-blue-600 text-white shadow' : 'text-slate-500 hover:text-blue-600')}>
                {t.icon} {t.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {view === 'user' ? <UserDistribution />
        : view === 'quickbi' ? <QbiDistribution />
        : <ResourceDistribution key={view} kind={view} />}
    </div>
  );
};

export default AgentDistribution;
