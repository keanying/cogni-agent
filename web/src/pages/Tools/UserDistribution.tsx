// -*- coding: utf-8 -*-
/**
 * 分发中心-按用户管理(15 号文档 T9:人视角;0074 起含 Skill)。
 * 以「人」为维度查看/管理该用户对全部 Agent 与 Skill 的分发状态:
 *   - assigned(已指派 vN):该用户命中某指派版本,正常可用
 *   - not_distributed(未分发,红标):有已发布版本但未指派给该用户 -> fail-closed,
 *     调用报 agent_not_distributed / skill_not_distributed
 *   - no_published(灰):还没有任何已发布版本
 * 支持直接为用户指派/切换/解除版本(scope 仅 user,无默认回落)。
 *
 * Agent 与 Skill 放同一屏而不是两个页面:管理员的问题是「这个人能用什么」,
 * 按资源类型拆开就得来回切,反而看不全。
 */
import React, { useMemo, useState } from 'react';
import {
  Search, Loader2, Bot, UsersRound, AlertTriangle, CheckCircle2, CircleDashed, X, Send, Zap,
  Database, ShieldAlert,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';

interface PickedUser { id: string; name: string; account: string; avatar_url?: string | null; role?: string | null; }

/** 三类资源在这一屏里的统一行模型 —— 「这个人能用什么」跟资源是什么无关,只有文案不同。
 *  Quick BI 是特例:它没有版本,状态多一个 open(该连接未启用白名单,谁可见谁能用)。 */
type RowKind = api.AssignableType | 'quickbi';
interface Row {
  kind: RowKind;
  id: string;
  name: string;
  subtitle?: string | null;
  state: api.AgentByUserState | api.QbiGrantState;
  published_count: number;
  assigned_version?: number | null;
  assigned_version_id?: string | null;
  assignment_id?: string | null;
  note?: string | null;
}

interface AssignDraft { row: Row; versions: api.ResourceVersion[]; open: boolean; }

const KIND_LABEL: Record<RowKind, string> = { agent: 'Agent', skill: 'Skill', quickbi: 'Quick BI' };

const UserDistribution: React.FC = () => {
  const [keyword, setKeyword] = useState('');
  const [candidates, setCandidates] = useState<any[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [picked, setPicked] = useState<PickedUser | null>(null);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [userInfo, setUserInfo] = useState<api.AgentByUserResult['user'] | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [draft, setDraft] = useState<AssignDraft | null>(null);
  const [draftVersion, setDraftVersion] = useState<string>('');
  const [draftNote, setDraftNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [kindFilter, setKindFilter] = useState<'all' | RowKind>('all');

  const searchUsers = async () => {
    if (!keyword.trim()) { setCandidates([]); return; }
    setSearching(true);
    try {
      const res = await api.listUsers({ keyword: keyword.trim(), per_page: 10 } as any);
      const items = ((res as any).items || (res as any).users || []).filter((u: any) => u && u.id);
      setCandidates(items);
    } catch (e: any) {
      toast.error(e.message || '用户搜索失败');
      setCandidates([]);
    } finally { setSearching(false); }
  };

  const pick = async (u: any) => {
    setPicked({ id: u.id, name: u.name, account: u.account, avatar_url: u.avatar_url, role: u.role });
    setRows(null);
    setLoading(true);
    try {
      // 两类资源并行拉:技能接口在老后端上不存在时不拖垮整屏(allSettled)
      const [ag, sk, qb] = await Promise.allSettled([
        api.listAgentsByUser(u.id),
        api.listSkillsByUser(u.id),
        api.qbiGrantsByUser(u.id),
      ]);
      const out: Row[] = [];
      if (ag.status === 'fulfilled') {
        setUserInfo(ag.value.user);
        out.push(...(ag.value.items || []).map((it): Row => ({
          kind: 'agent', id: it.agent_id, name: it.name, subtitle: it.agent_id,
          state: it.state, published_count: it.published_count,
          assigned_version: it.assigned_version, assigned_version_id: it.assigned_version_id,
          assignment_id: it.assignment_id, note: it.note,
        })));
      }
      if (sk.status === 'fulfilled') {
        setUserInfo((prev) => prev || sk.value.user);
        out.push(...(sk.value.items || []).map((it): Row => ({
          kind: 'skill', id: it.skill_id, name: it.name, subtitle: it.description || it.skill_id,
          state: it.state, published_count: it.published_count,
          assigned_version: it.assigned_version, assigned_version_id: it.assigned_version_id,
          assignment_id: it.assignment_id, note: it.note,
        })));
      }
      if (qb.status === 'fulfilled') {
        setUserInfo((prev) => prev || qb.value.user);
        out.push(...(qb.value.items || []).map((it): Row => ({
          kind: 'quickbi', id: it.connection_id, name: it.name,
          subtitle: it.connection_id, state: it.state, published_count: it.granted_count,
          assignment_id: it.grant_id, note: it.note,
        })));
      }
      if (ag.status === 'rejected' && sk.status === 'rejected' && qb.status === 'rejected') {
        toast.error('加载该用户的分发情况失败');
      }
      setRows(out);
    } finally { setLoading(false); }
  };

  const openAssign = async (row: Row) => {
    if (row.kind === 'quickbi') {
      // Quick BI 没有版本可选,直接分配即可
      setBusyId(row.id);
      try {
        const r = await api.qbiGrantConnection(row.id, { user_id: picked!.id });
        toast.success(r.message || '已分配');
        if (picked) await pick(picked);
      } catch (e: any) {
        toast.error(e.message || '分配失败');
      } finally { setBusyId(null); }
      return;
    }
    setDraft({ row, versions: [], open: true });
    setDraftVersion('');
    setDraftNote('');
    try {
      const { items } = await api.listResourceVersions(row.kind as api.AssignableType, row.id);
      const published = (items || []).filter((v) => v.status === 'published');
      if (!published.length) {
        toast.warning(`该 ${KIND_LABEL[row.kind]} 暂无已发布版本，无法指派`);
        setDraft(null);
        return;
      }
      const cur = row.assigned_version_id;
      const next = published.find((v) => v.id === cur) || published[0];
      setDraft({ row, versions: published, open: true });
      setDraftVersion(next.id);
    } catch (e: any) {
      toast.error(e.message || '加载版本失败');
      setDraft(null);
    }
  };

  const submitAssign = async () => {
    if (!draft || !draftVersion) { toast.warning('请选择要指派的版本'); return; }
    // 版本弹层只对 agent/skill 开(Quick BI 没有版本,在 openAssign 里就直接分配完了)
    if (draft.row.kind === 'quickbi') { setDraft(null); return; }
    const kind: api.AssignableType = draft.row.kind;
    setSaving(true);
    try {
      const r = await api.upsertAssignment(draft.row.id, {
        user_id: picked!.id, version_id: draftVersion, note: draftNote || undefined,
      }, kind);
      toast.success(r.message || '指派成功');
      setDraft(null);
      if (picked) await pick(picked);
    } catch (e: any) {
      toast.error(e.message || '指派失败');
    } finally { setSaving(false); }
  };

  const removeAsg = async (row: Row) => {
    if (!row.assignment_id) return;
    setBusyId(row.id);
    try {
      const r = row.kind === 'quickbi'
        ? await api.qbiRevokeGrant(row.id, row.assignment_id)
        : await api.removeAssignment(row.id, row.assignment_id, row.kind);
      toast.warning(r.message || '已解除指派');
      if (picked) await pick(picked);
    } catch (e: any) {
      toast.error(e.message || '解除失败');
    } finally { setBusyId(null); }
  };

  const visible = useMemo(
    () => (rows || []).filter((r) => kindFilter === 'all' || r.kind === kindFilter),
    [rows, kindFilter],
  );

  const totals = useMemo(() => ({
    assigned: visible.filter((x) => x.state === 'assigned' || x.state === 'granted').length,
    missing: visible.filter((x) => x.state === 'not_distributed' || x.state === 'not_granted').length,
    none: visible.filter((x) => x.state === 'no_published').length,
    open: visible.filter((x) => x.state === 'open').length,
  }), [visible]);

  const stateUi = (state: Row['state']) => {
    if (state === 'open') return { icon: <ShieldAlert size={13} className="text-amber-500" />, cls: 'bg-amber-50 text-amber-600 border-amber-200', label: '未启用白名单' };
    if (state === 'granted') return { icon: <CheckCircle2 size={13} className="text-emerald-500" />, cls: 'bg-emerald-50 text-emerald-700 border-emerald-200', label: '已分配' };
    if (state === 'not_granted') return { icon: <AlertTriangle size={13} className="text-red-500" />, cls: 'bg-red-50 text-red-600 border-red-200', label: '未分配' };
    if (state === 'assigned') return { icon: <CheckCircle2 size={13} className="text-emerald-500" />, cls: 'bg-emerald-50 text-emerald-700 border-emerald-200', label: '已指派' };
    if (state === 'not_distributed') return { icon: <AlertTriangle size={13} className="text-red-500" />, cls: 'bg-red-50 text-red-600 border-red-200', label: '未分发' };
    return { icon: <CircleDashed size={13} className="text-slate-400" />, cls: 'bg-slate-100 text-slate-500 border-slate-200', label: '暂无发布' };
  };

  const counts = useMemo(() => ({
    agent: (rows || []).filter((r) => r.kind === 'agent').length,
    skill: (rows || []).filter((r) => r.kind === 'skill').length,
    quickbi: (rows || []).filter((r) => r.kind === 'quickbi').length,
  }), [rows]);

  return (
    <div className="grid lg:grid-cols-[340px_1fr] gap-6 items-start min-w-0">
      {/* ── 左:选用户 ── */}
      <Card className="border-slate-200 shadow-sm">
        <CardHeader className="pb-3">
          <CardTitle className="text-xs font-black uppercase tracking-widest text-slate-500 flex items-center gap-2">
            <UsersRound size={14} className="text-blue-600" /> 选择用户
          </CardTitle>
        </CardHeader>
        <CardContent className="p-4 space-y-3">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
              <Input value={keyword} onChange={(e) => setKeyword(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && searchUsers()}
                placeholder="搜索用户(姓名/账号/手机号)..." className="pl-9 h-9 bg-white border-slate-200 rounded-xl text-sm" />
            </div>
            <Button size="sm" className="h-9 rounded-xl bg-blue-600 hover:bg-blue-700 text-xs font-bold px-3"
              disabled={searching} onClick={searchUsers}>
              {searching ? <Loader2 size={13} className="animate-spin" /> : '搜索'}
            </Button>
          </div>

          {candidates && candidates.length === 0 && !searching && (
            <div className="text-center py-8 text-slate-300 text-xs">未找到用户</div>
          )}
          {candidates && candidates.length > 0 && (
            <div className="space-y-2 max-h-[46vh] overflow-y-auto pr-1 custom-scrollbar">
              {candidates.map((u: any) => (
                <button key={u.id} onClick={() => pick(u)}
                  className={cn('w-full text-left rounded-2xl border p-3 transition-all flex items-center gap-3',
                    picked?.id === u.id ? 'border-blue-500 bg-blue-50/60 shadow-md shadow-blue-100'
                      : 'border-slate-100 bg-white hover:border-blue-200')}>
                  <div className="h-9 w-9 rounded-xl bg-blue-100 text-blue-700 flex items-center justify-center font-black text-sm shrink-0">
                    {(u.name || u.account || '?')[0]}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-black text-slate-800 truncate">{u.name || u.account}</div>
                    <div className="text-[10px] text-slate-400 font-mono truncate">{u.account}{u.role ? ` · ${u.role}` : ''}</div>
                  </div>
                  {picked?.id === u.id && <CheckCircle2 size={16} className="text-blue-600 shrink-0" />}
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── 右:该用户视角下的 Agent / Skill 分发 ── */}
      <div className="space-y-4 min-w-0">
        {!picked ? (
          <Card className="border-slate-200 shadow-sm">
            <CardContent className="p-14 text-center text-slate-300 text-sm">
              <UsersRound className="mx-auto mb-3 text-slate-200" size={32} />
              在左侧选择一位用户,查看其可见的 Agent / Skill 与分发状态
            </CardContent>
          </Card>
        ) : loading ? (
          <Card className="border-slate-200 shadow-sm">
            <CardContent className="p-14 text-center text-slate-300"><Loader2 size={22} className="mx-auto animate-spin" /></CardContent>
          </Card>
        ) : (
          <>
            <Card className="border-slate-200 shadow-sm overflow-hidden">
              <CardContent className="p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <div className="flex items-center gap-3 min-w-0">
                  <div className="h-11 w-11 rounded-2xl bg-blue-600 text-white flex items-center justify-center font-black text-lg shrink-0 shadow-lg shadow-blue-200">
                    {(userInfo?.name || userInfo?.account || picked.name || '?')[0]}
                  </div>
                  <div className="min-w-0">
                    <div className="font-black text-slate-900">{userInfo?.name || picked.name}</div>
                    <div className="text-[11px] text-slate-400 font-mono truncate">{userInfo?.account || picked.account}</div>
                  </div>
                </div>
                <div className="flex items-center gap-3 shrink-0 text-[11px] font-bold">
                  <Badge className="bg-emerald-50 text-emerald-700 border-emerald-200 rounded-full px-2.5 py-1 gap-1"><CheckCircle2 size={11} /> 已指派 {totals.assigned}</Badge>
                  <Badge className="bg-red-50 text-red-600 border-red-200 rounded-full px-2.5 py-1 gap-1"><AlertTriangle size={11} /> 未分发 {totals.missing}</Badge>
                  <Badge className="bg-slate-100 text-slate-500 border-slate-200 rounded-full px-2.5 py-1">暂无发布 {totals.none}</Badge>
                  {totals.open > 0 && (
                    <Badge className="bg-amber-50 text-amber-600 border-amber-200 rounded-full px-2.5 py-1 gap-1">
                      <ShieldAlert size={11} /> 未启用白名单 {totals.open}
                    </Badge>
                  )}
                </div>
              </CardContent>
            </Card>

            {/* 资源类型筛选:默认全看,数量多了再按类型收窄。
                窄屏横向滚动、标签不拆词 —— 不加的话「Quick BI」会被拆成两行 */}
            <div className="-mx-1 overflow-x-auto px-1 pb-1">
            <div className="inline-flex rounded-xl border border-slate-200 bg-white p-1 shadow-sm">
              {([['all', '全部', counts.agent + counts.skill + counts.quickbi],
                 ['agent', 'Agent', counts.agent],
                 ['skill', 'Skill', counts.skill],
                 ['quickbi', 'Quick BI', counts.quickbi]] as const).map(([k, label, n]) => (
                <button key={k} onClick={() => setKindFilter(k as any)}
                  className={cn('shrink-0 whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-black transition-all',
                    kindFilter === k ? 'bg-blue-600 text-white shadow' : 'text-slate-500 hover:text-blue-600')}>
                  {label} <span className="font-mono opacity-70">{n}</span>
                </button>
              ))}
            </div>
            </div>

            <Card className="border-slate-200 shadow-sm">
              <CardContent className="p-4">
                {visible.length === 0 ? (
                  <div className="text-center py-10 text-slate-300 text-sm">该用户暂无可见资源</div>
                ) : (
                  <div className="space-y-2">
                    {visible.map((item) => {
                      const st = stateUi(item.state);
                      const isSkill = item.kind === 'skill';
                      const isQbi = item.kind === 'quickbi';
                      return (
                        <div key={`${item.kind}:${item.id}`}
                          className="rounded-2xl border border-slate-100 bg-white p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                          <div className="flex items-center gap-3 min-w-0">
                            <div className={cn('h-10 w-10 rounded-xl flex items-center justify-center font-black shrink-0',
                              isQbi ? 'bg-cyan-50 text-cyan-600'
                                : isSkill ? 'bg-amber-50 text-amber-600' : 'bg-indigo-50 text-indigo-600')}>
                              {isQbi ? <Database size={16} /> : isSkill ? <Zap size={16} /> : item.name[0]}
                            </div>
                            <div className="min-w-0">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="text-sm font-black text-slate-800 truncate">{item.name}</span>
                                <Badge variant="outline" className={cn('rounded-full text-[9px] font-black border',
                                  isQbi ? 'bg-cyan-50 text-cyan-600 border-cyan-200'
                                    : isSkill ? 'bg-amber-50 text-amber-600 border-amber-200'
                                      : 'bg-indigo-50 text-indigo-600 border-indigo-200')}>
                                  {KIND_LABEL[item.kind]}
                                </Badge>
                                <Badge variant="outline" className={cn('rounded-full text-[9px] font-black border', st.cls)}>
                                  {st.icon} {st.label}
                                </Badge>
                              </div>
                              <div className="text-[10px] text-slate-400 font-mono mt-0.5 truncate">{item.subtitle}</div>
                              {item.state === 'assigned' && (
                                <div className="text-[11px] font-bold text-emerald-600 mt-1">
                                  命中 v{item.assigned_version}
                                  {item.note ? <span className="font-normal text-slate-400"> · {item.note}</span> : null}
                                </div>
                              )}
                              {item.state === 'granted' && (
                                <div className="text-[11px] font-bold text-emerald-600 mt-1">
                                  已分配该连接
                                  {item.note ? <span className="font-normal text-slate-400"> · {item.note}</span> : null}
                                </div>
                              )}
                              {item.state === 'not_granted' && (
                                <div className="text-[11px] text-red-500 mt-1">
                                  该连接已是白名单制({item.published_count} 人),此用户不在名单里 —— 看不到、也用不了
                                </div>
                              )}
                              {item.state === 'open' && (
                                <div className="text-[11px] text-amber-600 mt-1">
                                  该连接未启用白名单:按景区/可见性判定,现在就能用。分配一次会把它切成白名单制
                                </div>
                              )}
                              {item.state === 'not_distributed' && (
                                <div className="text-[11px] text-red-500 mt-1">
                                  已发布 {item.published_count} 版但未指派给该用户 —— 调用将报{' '}
                                  {isSkill ? 'skill_not_distributed' : 'agent_not_distributed'}(fail-closed)
                                </div>
                              )}
                              {item.state === 'no_published' && (
                                <div className="text-[11px] text-slate-400 mt-1">尚无已发布版本</div>
                              )}
                            </div>
                          </div>
                          <div className="flex items-center gap-2 shrink-0">
                            {(item.state === 'assigned' || item.state === 'granted') && (
                              <>
                                {item.state === 'assigned' && (
                                  <Button size="sm" variant="outline" className="h-8 rounded-lg text-[11px] font-bold text-blue-600 border-blue-200 hover:bg-blue-50 gap-1"
                                    onClick={() => openAssign(item)} disabled={busyId === item.id}>
                                    <Send size={11} /> 切换版本
                                  </Button>
                                )}
                                <Button size="sm" variant="outline" className="h-8 rounded-lg text-[11px] font-bold text-red-500 border-red-200 hover:bg-red-50 gap-1"
                                  onClick={() => removeAsg(item)} disabled={busyId === item.id}>
                                  {busyId === item.id ? <Loader2 size={11} className="animate-spin" /> : <X size={11} />} 解除
                                </Button>
                              </>
                            )}
                            {(item.state === 'not_distributed' || item.state === 'no_published'
                              || item.state === 'not_granted' || item.state === 'open') && (
                              <Button size="sm" className="h-8 rounded-lg text-[11px] font-bold bg-blue-600 hover:bg-blue-700 text-white gap-1"
                                onClick={() => openAssign(item)} disabled={item.state === 'no_published' || busyId === item.id}>
                                {busyId === item.id ? <Loader2 size={11} className="animate-spin" /> : null}
                                {item.state === 'no_published' ? '暂无法指派'
                                  : item.state === 'open' ? '分配给该用户(将转白名单)'
                                    : isQbi ? '分配给该用户' : '指派给该用户'}
                              </Button>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </CardContent>
            </Card>
          </>
        )}
      </div>

      {/* ── 指派/切换版本弹层 ── */}
      {draft?.open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm" onClick={() => !saving && setDraft(null)}>
          <div className="w-[480px] max-w-[92vw] rounded-3xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-lg font-black text-slate-900 flex items-center gap-2">
                  {draft.row.kind === 'skill'
                    ? <Zap size={18} className="text-amber-500" />
                    : <Bot size={18} className="text-blue-600" />}
                  {draft.row.name}
                </h3>
                <p className="text-xs text-slate-400 mt-0.5">
                  为用户 <b className="text-slate-600">{picked?.name}</b> 指派{KIND_LABEL[draft.row.kind]}版本(按人分发,无默认回落)
                </p>
              </div>
              <button onClick={() => !saving && setDraft(null)} className="text-slate-300 hover:text-slate-500"><X size={18} /></button>
            </div>

            <div className="mt-4 space-y-1 max-h-[40vh] overflow-y-auto pr-1 custom-scrollbar">
              <div className="text-[11px] font-black text-slate-400 uppercase tracking-wider mb-2">选择版本</div>
              {draft.versions.map((v) => (
                <label key={v.id} className={cn(
                  'flex items-start gap-3 rounded-xl border p-3 cursor-pointer transition-all',
                  draftVersion === v.id ? 'border-blue-500 bg-blue-50/70' : 'border-slate-100 hover:border-blue-200',
                )}>
                  <input type="radio" className="mt-0.5 accent-[#1677ff]" checked={draftVersion === v.id}
                    onChange={() => setDraftVersion(v.id)} />
                  <div className="min-w-0">
                    <div className="text-sm font-black text-slate-800">v{v.version}</div>
                    <div className="text-[11px] text-slate-400 truncate">
                      {new Date(v.published_at || v.created_at).toLocaleString('zh-CN')}
                    </div>
                  </div>
                </label>
              ))}
            </div>

            <textarea rows={2} value={draftNote}
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setDraftNote(e.target.value)}
              placeholder="备注(可选):如「VIP 客户,限景区 A」"
              className="mt-3 w-full h-auto rounded-xl text-sm border border-slate-200 bg-slate-50 px-3 py-2 outline-none focus:border-blue-400 resize-none" />

            <div className="flex justify-end gap-2 mt-4">
              <Button variant="outline" size="sm" className="rounded-xl text-xs font-bold" disabled={saving} onClick={() => setDraft(null)}>取消</Button>
              <Button size="sm" className="rounded-xl text-xs font-bold bg-blue-600 hover:bg-blue-700 text-white gap-1.5"
                disabled={saving || !draftVersion} onClick={submitAssign}>
                {saving ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />} 确认指派
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default UserDistribution;
