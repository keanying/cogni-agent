// -*- coding: utf-8 -*-
/**
 * 版本指派面板(15 号文档 D9-A;0074 起 Agent / Skill 共用)。
 * 分发粒度收敛到「人」:scope 仅 user(scope_id = 用户 id),scenic 维度已下线;
 * 无默认版本:解除指派 = 该用户 fail-closed(agent_not_distributed /
 * skill_not_distributed),需重新指派才恢复。
 * resourceType 决定打哪套接口(/agents/... 或 /skills/...),其余交互完全一致 ——
 * 指派这件事本身跟资源是什么没关系。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Users, Plus, X, Search, Loader2, Trash2, ArrowRight, UserRound, GitBranch, RefreshCw,
} from 'lucide-react';
import { Button } from '../ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/card';
import { Badge } from '../ui/badge';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { confirmDialog } from '../ui/confirm';

interface Props {
  agentId: string;
  /** 0074:资源类型(默认 agent);skill 时打 /admin/skills/... 那套接口 */
  resourceType?: api.AssignableType;
  /** 指派变更后通知外层刷新(分发中心徽标/已发布版本提示) */
  onChanged?: () => void;
  /** 父级已拉取的版本/指派数据,挂载时直接复用,避免重复请求 */
  initialAssignments?: api.ResourceAssignment[];
  initialVersions?: api.ResourceVersion[];
}

/** 角色中文名(展示用,未知角色回退原文) */
const ROLE_LABEL: Record<string, string> = {
  super_admin: '超级管理员',
  group_admin: '集团管理员',
  group_developer: '集团开发者',
  group_viewer: '集团观察者',
  scenic_admin: '景区管理员',
  scenic_operator: '景区运营',
  scenic_viewer: '景区观察者',
};

interface UserOption { id: string; name: string; desc: string; }

/** 用户静态字典缓存:与 agent 无关,全局共享;分发中心切换/后退复用,SPA 会话内只请求一次 */
let staticDictCache: { userMap: Record<string, any> } | null = null;
let staticDictPromise: Promise<{ userMap: Record<string, any> }> | null = null;

const AssignmentPanel: React.FC<Props> = ({ agentId, resourceType = 'agent', onChanged,
                                           initialAssignments, initialVersions }) => {
  const resLabel = resourceType === 'skill' ? 'Skill' : 'Agent';
  const [assignments, setAssignments] = useState<api.ResourceAssignment[]>(initialAssignments ?? []);
  const [versions, setVersions] = useState<api.ResourceVersion[]>(
    initialVersions ? initialVersions.filter((x) => x.status === 'published') : [],
  );
  /** 用户详情 map(id → 用户对象),用于表格展示姓名/账号/手机号/角色/景区 */
  const [userMap, setUserMap] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(initialAssignments === undefined || initialVersions === undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  // 弹窗表单(user-only)
  const [keyword, setKeyword] = useState('');
  const [userOptions, setUserOptions] = useState<UserOption[]>([]);
  const [scopeId, setScopeId] = useState('');
  const [versionId, setVersionId] = useState('');
  const [note, setNote] = useState('');
  // 覆盖已有指派时的影响面确认
  const [confirming, setConfirming] = useState<{ old: api.ResourceAssignment; scopeName: string; impact: string } | null>(null);
  /** 表格搜索关键字:按姓名/账号/手机号/用户ID 过滤已指派记录 */
  const [tableKeyword, setTableKeyword] = useState('');
  /** 单条改版弹窗:行数据 + 目标版本/备注 */
  const [editing, setEditing] = useState<api.ResourceAssignment | null>(null);
  const [editVersionId, setEditVersionId] = useState('');
  const [editNote, setEditNote] = useState('');
  /** 批量升级到最新版:先列出人员清单确认,再逐条切换 */
  const [batchOpen, setBatchOpen] = useState(false);

  /** 已发布版本按版本号降序:[0] 即最新版本 */
  const sortedVersions = useMemo(
    () => [...versions].sort((x, y) => (y.version ?? 0) - (x.version ?? 0)),
    [versions],
  );
  const latest = sortedVersions[0] ?? null;
  /** 落后于最新版的指派(15 号无默认版本:钉在哪版就用哪版,不会自动跟上) */
  const lagging = useMemo(
    () => (latest
      ? assignments.filter((a) => a.version_id !== latest.id && (a.version ?? 0) < (latest.version ?? 0))
      : []),
    [assignments, latest],
  );

  // 静态字典(用户)与 agent 无关,挂载时加载一次;命中模块级缓存则 0 请求
  const loadStatic = useCallback(async () => {
    if (staticDictCache) {
      setUserMap(staticDictCache.userMap);
      return;
    }
    if (!staticDictPromise) {
      staticDictPromise = (async () => {
        const users = await api.listUsers().catch(() => []);
        const userMap: Record<string, any> = {};
        const userArr = Array.isArray(users) ? users : (users?.items || []);
        userArr.forEach((u: any) => { if (u?.id) userMap[u.id] = u; });
        staticDictCache = { userMap };
        return staticDictCache;
      })().catch((e) => {
        staticDictPromise = null; // 失败清除缓存标记,允许下次重试
        throw e;
      });
    }
    try {
      const cache = await staticDictPromise;
      setUserMap(cache.userMap);
    } catch (e: any) {
      toast.error(e.message || '指派列表加载失败');
    }
  }, []);

  // 指派/版本核心数据:父级未提供初始数据时全量加载;操作(指派/解除)后调用刷新
  const loadCore = useCallback(async () => {
    try {
      const [a, v] = await Promise.all([
        api.listAssignments(agentId, resourceType),
        api.listResourceVersions(resourceType, agentId),
      ]);
      setAssignments(a.items);
      setVersions(v.items.filter((x) => x.status === 'published'));
    } catch (e: any) {
      toast.error(e.message || '指派列表加载失败');
    }
  }, [agentId, resourceType]);

  useEffect(() => { loadStatic(); }, [loadStatic]);

  useEffect(() => {
    if (initialAssignments === undefined || initialVersions === undefined) {
      setLoading(true);
      loadCore().finally(() => setLoading(false));
    } else {
      setAssignments(initialAssignments);
      setVersions(initialVersions.filter((x) => x.status === 'published'));
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, resourceType, initialAssignments, initialVersions, loadCore]);

  const [userNames, setUserNames] = useState<Record<string, string>>({});
  const userName = (id: string) => userMap[id]?.name || userNames[id] || id;

  const openModal = () => {
    setOpen(true);
    setKeyword(''); setUserOptions([]);
    setScopeId(''); setVersionId(versions[0]?.id || ''); setNote('');
  };

  const searchUsers = async (kw: string) => {
    setKeyword(kw);
    if (!kw.trim()) { setUserOptions([]); return; }
    try {
      const r: any = await api.listUsers(kw.trim());
      const items = Array.isArray(r) ? r : (r.items || []);
      setUserOptions(items.map((u: any) => ({ id: u.id, name: u.name, desc: `${u.account || ''} · ${u.role || ''}` })));
    } catch (e: any) {
      toast.error(e.message || '搜索失败');
    }
  };

  const submit = async (sid: string, vid: string, nt: string) => {
    setBusy('submit');
    try {
      const r = await api.upsertAssignment(agentId, {
        user_id: sid,
        version_id: vid,
        note: nt || undefined,
      }, resourceType);
      const opt = userOptions.find((o) => o.id === sid);
      if (opt) setUserNames((prev) => ({ ...prev, [sid]: opt.name }));
      toast.success(r.message || '指派成功');
      setOpen(false); setConfirming(null);
      loadCore();
      onChanged?.();
    } catch (e: any) {
      toast.error(e.message || '指派失败');
    } finally {
      setBusy(null);
    }
  };

  const handleConfirm = () => {
    if (!scopeId) { toast.error('请选择要指派的用户'); return; }
    if (!versionId) { toast.error('请选择版本'); return; }
    const sid = scopeId;
    const old = assignments.find((x) => x.scope_id === sid);
    const selName = userOptions.find((o) => o.id === sid)?.name || userName(sid);
    const newVer = versions.find((v) => v.id === versionId);
    if (old) {
      setConfirming({
        old,
        scopeName: selName,
        impact: `${selName} 当前使用 v${old.version ?? '—'},切换后使用 v${newVer?.version ?? '—'}(15 号按人分发,仅影响该用户)。`,
      });
    } else {
      submit(sid, versionId, note);
    }
  };

  /** 按关键字过滤指派记录:匹配姓名/账号/手机号/ID */
  const filteredAssignments = useCallback(() => {
    const kw = tableKeyword.trim().toLowerCase();
    if (!kw) return assignments;
    return assignments.filter((a) => {
      const u = userMap[a.scope_id];
      if (!u) return false;
      return [u.name, u.account, u.phone, u.id].some((f) => f && String(f).toLowerCase().includes(kw));
    });
  }, [assignments, tableKeyword, userMap]);

  /** 打开单条改版弹窗:目标版本默认选最新,备注默认继承原值(留空不清空本字段) */
  const openEdit = (a: api.ResourceAssignment) => {
    setEditing(a);
    setEditVersionId(latest?.id || a.version_id);
    setEditNote(a.note || '');
  };

  /** 切版本:复用 upsert(同一 user 重复指派 = 覆盖),与「添加指派」同一套语义 */
  const submitSwitch = async () => {
    if (!editing) return;
    if (!editVersionId) { toast.error('请选择目标版本'); return; }
    if (editVersionId === editing.version_id) { toast.error('目标版本与当前版本相同'); return; }
    setBusy('switch');
    try {
      const r = await api.upsertAssignment(agentId, {
        user_id: editing.scope_id,
        version_id: editVersionId,
        note: editNote.trim() || undefined, // 空值不覆盖原备注(后端 if note: 才写)
      }, resourceType);
      toast.success(r.message || '版本已切换');
      setEditing(null);
      loadCore();
      onChanged?.();
    } catch (e: any) {
      toast.error(e.message || '切换版本失败');
    } finally {
      setBusy(null);
    }
  };

  /** 批量升级:逐条调用(单条失败不中断),完成后一次性刷新 */
  const submitBatch = async () => {
    if (!latest) return;
    setBusy('batch');
    let ok = 0; let fail = 0;
    for (const a of lagging) {
      try {
        await api.upsertAssignment(agentId, { user_id: a.scope_id, version_id: latest.id }, resourceType);
        ok += 1;
      } catch {
        fail += 1;
      }
    }
    if (fail === 0) toast.success(`已升级 ${ok} 位用户到 v${latest.version}`);
    else toast.warning(`升级完成:${ok} 位成功,${fail} 位失败(请检查其版本可见性)`);
    setBatchOpen(false);
    loadCore();
    onChanged?.();
    setBusy(null);
  };

  const removeAssignment = async (a: api.ResourceAssignment) => {
    if (!await confirmDialog({
      title: `解除「${userName(a.scope_id)}」的版本指派`,
      message: `无默认回落:解除后该用户对本 ${resLabel} 将不可用(调用报 ${resourceType === 'skill' ? 'skill' : 'agent'}_not_distributed),需重新指派才恢复。`,
      confirmLabel: '解除指派',
      danger: true,
    })) return;
    setBusy(a.id);
    try {
      const r = await api.removeAssignment(agentId, a.id, resourceType);
      toast.warning(r.message || '已解除指派');
      loadCore();
      onChanged?.();
    } catch (e: any) {
      toast.error(e.message || '解除失败');
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card className="border-slate-200 shadow-sm overflow-hidden">
      <CardHeader className="border-b bg-slate-50/50 py-3 px-6 flex flex-row items-center justify-between">
        <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500">
          <Users size={16} /> 版本指派(按用户)
        </CardTitle>
        <Button size="sm" className="h-9 gap-1.5 px-3 text-xs bg-blue-600 hover:bg-blue-700 shadow-sm shadow-blue-200" onClick={openModal}>
          <Plus size={14} /> 添加指派
        </Button>
      </CardHeader>
      <CardContent className="p-4 space-y-2">
        <div className="rounded-lg border border-blue-100 bg-blue-50/60 px-3 py-2 text-[11px] leading-relaxed text-blue-700">
          15 号起分发粒度收敛到<b>人</b>(无景区范围、无默认版本)。指派给某用户后该用户固定使用指定版本;
          <b> 未指派/解除指派的用户调用将报 agent_not_distributed</b>(fail-closed)。
        </div>
        {loading ? (
          <div className="flex items-center justify-center py-8 text-slate-300"><Loader2 size={18} className="animate-spin" /></div>
        ) : assignments.length === 0 ? (
          <div className="text-center py-10">
            <div className="text-xs text-slate-400">
              暂无指派:任何用户都未命中本 {resLabel} 的版本(未分发),调用将报{' '}
              {resourceType === 'skill' ? 'skill_not_distributed' : 'agent_not_distributed'}。指派后该用户才可用。
            </div>
            <Button size="sm" className="mt-3 h-9 gap-1.5 text-xs bg-blue-600 hover:bg-blue-700 shadow-sm shadow-blue-200" onClick={openModal}>
              <Plus size={13} /> 添加指派
            </Button>
          </div>
        ) : (
          <div className="space-y-2">
            {/* 表格搜索:按姓名/账号/手机号/用户ID 过滤 */}
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
              <input value={tableKeyword} onChange={(e) => setTableKeyword(e.target.value)}
                placeholder="搜索姓名 / 账号 / 手机号 / 用户ID..."
                className="w-full h-9 pl-9 pr-3 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 text-xs outline-none transition-all" />
            </div>
            {latest && lagging.length > 0 && (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50/70 px-3 py-2 text-[11px] text-amber-800">
                <span>
                  已发布最新版 <b>v{latest.version}</b>,
                  有 <b>{lagging.length}</b> 位用户仍钉在旧版本 —— 发布 ≠ 生效,需切换指派才生效。
                </span>
                <Button size="sm" variant="outline" className="h-7 gap-1.5 text-[11px] font-bold text-amber-700 border-amber-300 hover:bg-amber-100"
                  onClick={() => setBatchOpen(true)}>
                  <RefreshCw size={12} /> 一键升级这 {lagging.length} 位
                </Button>
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="text-left text-[10px] uppercase tracking-wider text-slate-400 border-b border-slate-100">
                    <th className="py-2 pr-3 font-bold">姓名</th>
                    <th className="py-2 pr-3 font-bold">账号</th>
                    <th className="py-2 pr-3 font-bold">手机号</th>
                    <th className="py-2 pr-3 font-bold">用户ID</th>
                    <th className="py-2 pr-3 font-bold">角色 / 景区</th>
                    <th className="py-2 pr-3 font-bold">版本</th>
                    <th className="py-2 pr-3 font-bold">操作人</th>
                    <th className="py-2 text-right font-bold">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredAssignments().map((a) => {
                    const user = userMap[a.scope_id];
                    const operator = userMap[a.created_by];
                    return (
                      <tr key={a.id} className="border-b border-slate-50 hover:bg-slate-50/50 transition-colors">
                        <td className="py-2.5 pr-3">
                          <div className="flex items-center gap-2">
                            <span className="h-6 w-6 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center shrink-0">
                              <UserRound size={12} />
                            </span>
                            <span className="text-[13px] font-bold text-slate-800 max-w-[120px] truncate">
                              {user?.name || '—'}
                            </span>
                          </div>
                        </td>
                        <td className="py-2.5 pr-3 text-[12px] text-slate-600 font-mono max-w-[90px] truncate">
                          {user?.account || '—'}
                        </td>
                        <td className="py-2.5 pr-3 text-[12px] text-slate-600 font-mono whitespace-nowrap">
                          {user?.phone || '—'}
                        </td>
                        <td className="py-2.5 pr-3 text-[11px] text-slate-400 font-mono whitespace-nowrap" title={user?.id || a.scope_id}>
                          {user?.id ? `${user.id.slice(0, 8)}…` : a.scope_id.slice(0, 8)}
                        </td>
                        <td className="py-2.5 pr-3 text-[12px] text-slate-600 font-medium max-w-[140px] truncate">
                          {(ROLE_LABEL[user?.role] || user?.role || '—')}{user?.scenic_name ? ` · ${user.scenic_name}` : ''}
                        </td>
                        <td className="py-2.5 pr-3">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <Badge variant="outline" className="rounded-full text-[11px] font-black bg-blue-50 text-blue-600 border-blue-200">
                              v{a.version ?? '—'}
                            </Badge>
                            {latest && a.version_id !== latest.id && (a.version ?? 0) < (latest.version ?? 0) && (
                              <span className="text-[10px] font-bold text-amber-600 whitespace-nowrap"
                                title="该用户仍钉在旧版本:发布 ≠ 生效,需切换指派才生效">
                                最新 v{latest.version}
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="py-2.5 pr-3 text-[11px] text-slate-500 max-w-[90px] truncate" title={operator?.name || a.created_by}>
                          {operator?.name || (a.created_by ? `${a.created_by.slice(0, 8)}…` : '—')}
                        </td>
                        <td className="py-2.5 text-right whitespace-nowrap">
                          <div className="inline-flex items-center gap-1">
                            <Button variant="ghost" size="icon" className="h-7 w-7 text-blue-500 hover:text-blue-700" disabled={!!busy}
                              onClick={() => openEdit(a)} title="切换该用户的版本">
                              {busy === 'switch' && editing?.id === a.id
                                ? <Loader2 size={13} className="animate-spin" /> : <GitBranch size={13} />}
                            </Button>
                            <Button variant="ghost" size="icon" className="h-7 w-7 text-rose-400 hover:text-rose-600" disabled={busy === a.id}
                              onClick={() => removeAssignment(a)} title="解除指派">
                              {busy === a.id ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                            </Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                  {filteredAssignments().length === 0 && (
                    <tr>
                      <td colSpan={8} className="py-8 text-center text-xs text-slate-400">未找到匹配「{tableKeyword}」的指派记录</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </CardContent>

      {/* 切换版本弹窗:针对已指派用户改钉另一个已发布版本(同一 user 重复指派 = 覆盖) */}
      {editing && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={() => !busy && setEditing(null)}>
          <div className="w-full max-w-md rounded-2xl bg-white shadow-2xl overflow-hidden" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between px-5 py-4 border-b bg-slate-50/50">
              <h3 className="text-base font-black text-slate-800 flex items-center gap-2"><GitBranch size={16} className="text-blue-600" /> 切换版本</h3>
              <button className="text-slate-400 hover:text-slate-600" onClick={() => setEditing(null)}><X size={18} /></button>
            </div>
            <div className="p-5 space-y-4">
              <div className="flex items-center gap-2 text-sm text-slate-700">
                <span className="h-7 w-7 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center shrink-0"><UserRound size={13} /></span>
                <span className="font-bold">{userName(editing.scope_id)}</span>
                <span className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-600 text-xs font-bold">当前 v{editing.version ?? '—'}</span>
                <ArrowRight size={14} className="text-blue-500" />
                <span className="px-2 py-0.5 rounded-md bg-blue-100 text-blue-600 text-xs font-bold">
                  目标 v{sortedVersions.find((v) => v.id === editVersionId)?.version ?? '—'}
                </span>
              </div>

              <div>
                <label className="text-xs font-bold text-slate-700 uppercase tracking-tight mb-2 block">目标版本(仅已发布可选,切换后立即生效)</label>
                <select value={editVersionId} onChange={(e) => setEditVersionId(e.target.value)}
                  className="w-full h-10 rounded-xl border border-slate-200 bg-white px-3 text-sm font-bold text-slate-700 outline-none focus:border-blue-500">
                  {sortedVersions.length === 0 && <option value="">暂无已发布版本,请先发布</option>}
                  {sortedVersions.map((v) => (
                    <option key={v.id} value={v.id}>
                      v{v.version}{v.id === latest?.id ? ' · 最新' : ''}{v.id === editing.version_id ? ' · 当前' : ''}
                      {v.assigned_count ? ` · 已指派 ${v.assigned_count} 位用户` : ''}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="text-xs font-bold text-slate-700 uppercase tracking-tight mb-2 block">备注(留空则保留原备注)</label>
                <input value={editNote} onChange={(e) => setEditNote(e.target.value)} placeholder="如:付费客户留在 v11"
                  className="w-full h-10 px-3 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 text-sm outline-none transition-all" />
              </div>

              <div className="rounded-lg border border-amber-100 bg-amber-50/60 px-3 py-2 text-[11px] leading-relaxed text-amber-700">
                仅影响该用户;15 号无默认版本,未切换的用户仍用旧版本副本的技能/工具参数。
              </div>

              <div className="flex gap-3 pt-1">
                <Button variant="outline" className="flex-1" onClick={() => setEditing(null)} disabled={!!busy}>取消</Button>
                <Button className="flex-1 bg-blue-600 hover:bg-blue-700 gap-2"
                  disabled={!!busy || !editVersionId || editVersionId === editing.version_id} onClick={submitSwitch}>
                  {busy === 'switch' ? <Loader2 size={15} className="animate-spin" /> : <GitBranch size={15} />} 确认切换
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 批量升级到最新版:先列清单再执行(逐个提交,失败不中断) */}
      {batchOpen && latest && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={() => !busy && setBatchOpen(false)}>
          <div className="w-full max-w-md rounded-2xl bg-white shadow-2xl overflow-hidden" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between px-5 py-4 border-b bg-amber-50">
              <h3 className="text-base font-black text-amber-800 flex items-center gap-2"><RefreshCw size={16} /> 升级到 v{latest.version}</h3>
              <button className="text-slate-400 hover:text-slate-600" onClick={() => setBatchOpen(false)}><X size={18} /></button>
            </div>
            <div className="p-5 space-y-3">
              <div className="text-xs text-slate-500 leading-relaxed">
                以下 {lagging.length} 位用户将被切换到最新版本,原版本不再对其生效:
              </div>
              <div className="max-h-60 overflow-y-auto rounded-xl border border-slate-100 divide-y divide-slate-50">
                {lagging.map((a) => (
                  <div key={a.id} className="flex items-center justify-between px-4 py-2 text-sm">
                    <span className="truncate text-slate-700">{userName(a.scope_id)}</span>
                    <span className="flex items-center gap-1.5 text-xs font-bold text-slate-500 shrink-0">
                      <span className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-600">v{a.version ?? '—'}</span>
                      <ArrowRight size={12} className="text-blue-500" />
                      <span className="px-2 py-0.5 rounded-md bg-blue-100 text-blue-600">v{latest.version}</span>
                    </span>
                  </div>
                ))}
              </div>
              <div className="flex gap-3 pt-1">
                <Button variant="outline" className="flex-1" onClick={() => setBatchOpen(false)} disabled={!!busy}>取消</Button>
                <Button className="flex-1 bg-amber-500 hover:bg-amber-600 text-white gap-2" disabled={!!busy} onClick={submitBatch}>
                  {busy === 'batch' ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />} 确认升级 {lagging.length} 位
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 指派弹窗(user-only) */}
      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={() => !busy && setOpen(false)}>
          <div className="w-full max-w-md rounded-2xl bg-white shadow-2xl overflow-hidden" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between px-5 py-4 border-b bg-slate-50/50">
              <h3 className="text-base font-black text-slate-800 flex items-center gap-2"><ArrowRight size={16} className="text-blue-600" /> 指派给用户</h3>
              <button className="text-slate-400 hover:text-slate-600" onClick={() => setOpen(false)}><X size={18} /></button>
            </div>
            <div className="p-5 space-y-4">
              {/* 选择用户 */}
              <div>
                <label className="text-xs font-bold text-slate-700 uppercase tracking-tight mb-2 block">选择用户(分发粒度=人)</label>
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
                  <input value={keyword} onChange={(e) => searchUsers(e.target.value)}
                    placeholder="搜索用户姓名/账号/手机号..."
                    className="w-full h-10 pl-9 pr-3 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 text-sm outline-none transition-all" />
                </div>
                {userOptions.length > 0 && (
                  <div className="mt-2 max-h-48 overflow-y-auto rounded-xl border border-slate-100 divide-y divide-slate-50">
                    {userOptions.map((o) => (
                      <button key={o.id} onClick={() => setScopeId(o.id)}
                        className={`w-full flex items-center justify-between px-4 py-2.5 text-left text-sm transition-colors
                          ${scopeId === o.id ? 'bg-blue-50 text-blue-700 font-bold' : 'text-slate-600 hover:bg-slate-50'}`}>
                        <span className="truncate">{o.name}</span>
                        <span className="text-[10px] text-slate-400 shrink-0 ml-2">{o.desc}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* 版本选择 */}
              <div>
                <label className="text-xs font-bold text-slate-700 uppercase tracking-tight mb-2 block">版本</label>
                <select value={versionId} onChange={(e) => setVersionId(e.target.value)}
                  className="w-full h-10 rounded-xl border border-slate-200 bg-white px-3 text-sm font-bold text-slate-700 outline-none focus:border-blue-500">
                  {versions.length === 0 && <option value="">暂无已发布版本,请先发布</option>}
                  {versions.map((v) => (
                    <option key={v.id} value={v.id}>v{v.version}{v.assigned_count ? ` · 已指派 ${v.assigned_count} 位用户` : ''}</option>
                  ))}
                </select>
              </div>

              {/* 备注 */}
              <div>
                <label className="text-xs font-bold text-slate-700 uppercase tracking-tight mb-2 block">备注(可选)</label>
                <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="如:VIP 客户,限景区 A"
                  className="w-full h-10 px-3 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 text-sm outline-none transition-all" />
              </div>

              <div className="flex gap-3 pt-1">
                <Button variant="outline" className="flex-1" onClick={() => setOpen(false)} disabled={!!busy}>取消</Button>
                <Button className="flex-1 bg-blue-600 hover:bg-blue-700 gap-2" disabled={!!busy || versions.length === 0} onClick={handleConfirm}>
                  {busy === 'submit' ? <Loader2 size={15} className="animate-spin" /> : <ArrowRight size={15} />} 确认指派
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 覆盖影响面确认 */}
      {confirming && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 backdrop-blur-sm p-4">
          <div className="w-full max-w-sm rounded-2xl bg-white shadow-2xl overflow-hidden">
            <div className="px-5 py-4 border-b border-amber-100 bg-amber-50">
              <h3 className="text-base font-black text-amber-800 flex items-center gap-2">
                <span className="h-8 w-8 rounded-full bg-amber-500 text-white flex items-center justify-center text-base">!</span>
                切换版本影响面确认
              </h3>
            </div>
            <div className="p-5 space-y-3">
              <div className="text-sm text-slate-600 leading-relaxed">
                该用户已有指派,切换将<b>立即生效</b>(仅影响该用户):
              </div>
              <div className="rounded-xl border border-amber-100 bg-amber-50/50 px-4 py-3 text-sm text-slate-700 space-y-1">
                <div className="flex items-center gap-2">
                  <span className="font-bold">{confirming.scopeName}</span>
                  <Badge variant="outline" className="rounded-full text-[10px] font-bold bg-white text-blue-600 border-blue-200">
                    用户
                  </Badge>
                </div>
                <div className="flex items-center gap-2 text-xs text-slate-500 font-bold">
                  原 <span className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-600">v{confirming.old.version ?? '—'}</span>
                  <ArrowRight size={14} className="text-amber-500" />
                  新 <span className="px-2 py-0.5 rounded-md bg-blue-100 text-blue-600">v{versions.find((v) => v.id === versionId)?.version ?? '—'}</span>
                </div>
              </div>
              <div className="text-xs text-slate-500 leading-relaxed">{confirming.impact}</div>
              <div className="flex gap-3 pt-1">
                <Button variant="outline" className="flex-1" onClick={() => setConfirming(null)} disabled={busy === 'submit'}>取消</Button>
                <Button className="flex-1 bg-amber-500 hover:bg-amber-600 text-white gap-2" disabled={busy === 'submit'}
                  onClick={() => { submit(confirming.old.scope_id, versionId, note); }}>
                  {busy === 'submit' ? <Loader2 size={15} className="animate-spin" /> : <ArrowRight size={15} />} 确认切换
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
};

export default AssignmentPanel;
