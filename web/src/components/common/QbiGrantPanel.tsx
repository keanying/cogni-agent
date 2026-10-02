// -*- coding: utf-8 -*-
/**
 * Quick BI 连接分配面板(0075)。
 *
 * 与 Agent/Skill 的版本指派刻意长得像，但分的东西不同：连接没有版本概念，
 * 分的是「这个人能不能用这套凭证」。所以这里没有版本下拉，只有人 + 备注。
 *
 * 一条分配都没有 = 该连接还没启用白名单，按景区 + 可见性判定（也就是老行为）；
 * 加了第一条就转白名单制 —— 这句话必须写在界面上，否则管理员会以为只是「加个人」。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Users, Plus, X, Search, Loader2, Trash2, ShieldCheck, ShieldAlert, Database } from 'lucide-react';
import { Button } from '../ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/card';
import { Badge } from '../ui/badge';
import { Input } from '../ui/input';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { confirmDialog } from '../ui/confirm';

const ROLE_LABEL: Record<string, string> = {
  super_admin: '超级管理员', group_admin: '集团管理员', group_developer: '集团开发者',
  group_viewer: '集团观察者', scenic_admin: '景区管理员', scenic_operator: '景区运营',
  scenic_viewer: '景区观察者',
};

interface Props {
  connectionId: string;
  connectionName: string;
  onChanged?: () => void;
}

const QbiGrantPanel: React.FC<Props> = ({ connectionId, connectionName, onChanged }) => {
  const [grants, setGrants] = useState<api.QbiGrant[]>([]);
  const [restricted, setRestricted] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [candidates, setCandidates] = useState<any[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [picked, setPicked] = useState<string>('');
  const [note, setNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [tableKeyword, setTableKeyword] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.qbiListGrants(connectionId);
      setGrants(res.items || []);
      setRestricted(!!res.restricted);
    } catch (e: any) {
      toast.error(e.message || '加载分配列表失败');
      setGrants([]);
    } finally {
      setLoading(false);
    }
  }, [connectionId]);

  useEffect(() => { load(); }, [load]);

  const searchUsers = async () => {
    setSearching(true);
    try {
      const res: any = await api.listUsers(
        keyword.trim() ? ({ keyword: keyword.trim(), per_page: 10 } as any) : ({ per_page: 10 } as any));
      setCandidates((res.items || res.users || res || []).filter((u: any) => u && u.id));
    } catch (e: any) {
      toast.error(e.message || '用户搜索失败');
      setCandidates([]);
    } finally { setSearching(false); }
  };

  const submit = async () => {
    if (!picked) { toast.warning('请选择要分配的用户'); return; }
    setSaving(true);
    try {
      const r = await api.qbiGrantConnection(connectionId, { user_id: picked, note: note || undefined });
      toast.success(r.message || '已分配');
      setOpen(false); setPicked(''); setNote(''); setKeyword(''); setCandidates(null);
      await load();
      onChanged?.();
    } catch (e: any) {
      toast.error(e.message || '分配失败');
    } finally { setSaving(false); }
  };

  const revoke = async (g: api.QbiGrant) => {
    const last = grants.length === 1;
    const ok = await confirmDialog({
      title: '解除分配?',
      message: last
        ? `解除后「${connectionName}」将没有任何分配,回到按景区/可见性判定——所有可见的人又都能用了。`
        : `解除后该用户将无法再使用「${connectionName}」(看不到、也拿不到数据)。`,
      confirmLabel: '解除',
      danger: true,
    });
    if (!ok) return;
    setBusy(g.id);
    try {
      const r = await api.qbiRevokeGrant(connectionId, g.id);
      toast.warning(r.message || '已解除');
      await load();
      onChanged?.();
    } catch (e: any) {
      toast.error(e.message || '解除失败');
    } finally { setBusy(null); }
  };

  const filtered = useMemo(() => {
    const k = tableKeyword.trim().toLowerCase();
    if (!k) return grants;
    return grants.filter((g) => [g.user_name, g.user_account, g.user_id, g.note]
      .some((v) => (v || '').toLowerCase().includes(k)));
  }, [grants, tableKeyword]);

  return (
    <Card className="border-slate-200 shadow-sm">
      <CardHeader className="border-b bg-slate-50/50 py-3 px-5 flex flex-row items-center justify-between gap-3">
        <CardTitle className="text-xs font-black uppercase tracking-widest text-slate-500 flex items-center gap-2">
          <Users size={14} className="text-blue-600" /> 连接分配
          <Badge variant="outline" className={cn('rounded-full text-[9px] font-black border ml-1',
            restricted ? 'bg-emerald-50 text-emerald-600 border-emerald-200'
              : 'bg-slate-100 text-slate-500 border-slate-200')}>
            {restricted ? <><ShieldCheck size={10} className="inline mr-0.5" />白名单 {grants.length} 人</>
              : <><ShieldAlert size={10} className="inline mr-0.5" />未启用白名单</>}
          </Badge>
        </CardTitle>
        <Button size="sm" className="h-8 rounded-xl bg-blue-600 hover:bg-blue-700 text-xs font-bold gap-1.5"
          onClick={() => { setOpen(true); setCandidates(null); setKeyword(''); searchUsers(); }}>
          <Plus size={13} /> 分配给用户
        </Button>
      </CardHeader>
      <CardContent className="p-4 space-y-3">
        <div className={cn('rounded-xl border px-3 py-2 text-[11px] leading-relaxed',
          restricted ? 'border-emerald-100 bg-emerald-50/60 text-emerald-700'
            : 'border-amber-100 bg-amber-50/60 text-amber-700')}>
          {restricted
            ? <>该连接已是<b>白名单制</b>：只有下面这些人能看到并使用它，其他人（含本景区同事）一律不可见。超级管理员不受限。</>
            : <>该连接<b>尚未启用白名单</b>：按景区 + 可见性判定，凡是能看到它的人都能用。<b>加入第一条分配后立即转为白名单制</b>，届时未被分配的人会失去访问。</>}
        </div>

        {grants.length > 3 && (
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
            <Input value={tableKeyword} onChange={(e) => setTableKeyword(e.target.value)}
              placeholder="按姓名 / 账号 / 备注过滤..." className="pl-9 h-9 rounded-xl text-sm" />
          </div>
        )}

        {loading ? (
          <div className="flex justify-center py-10 text-slate-300"><Loader2 size={20} className="animate-spin" /></div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-10 text-xs text-slate-400">
            {grants.length === 0
              ? '暂无分配:该连接按景区/可见性开放。要收紧它,给指定用户分配一次即可。'
              : '没有匹配的记录'}
          </div>
        ) : (
          <div className="space-y-2">
            {filtered.map((g) => (
              <div key={g.id} className="rounded-2xl border border-slate-100 bg-white p-3.5 flex items-center justify-between gap-3">
                <div className="flex items-center gap-3 min-w-0">
                  <div className="h-9 w-9 rounded-xl bg-blue-50 text-blue-600 flex items-center justify-center font-black text-sm shrink-0">
                    {(g.user_name || g.user_account || '?')[0]}
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-black text-slate-800 truncate">{g.user_name || g.user_account || g.user_id}</span>
                      {g.user_role && (
                        <Badge variant="outline" className="rounded-full text-[9px] font-bold text-slate-500 border-slate-200">
                          {ROLE_LABEL[g.user_role] || g.user_role}
                        </Badge>
                      )}
                      {g.user_scenic_id && (
                        <span className="text-[10px] text-slate-400 font-mono">{g.user_scenic_id}</span>
                      )}
                    </div>
                    <div className="text-[10px] text-slate-400 font-mono truncate">{g.user_account || g.user_id}</div>
                    {g.note && <div className="text-[11px] text-slate-500 mt-0.5 truncate">{g.note}</div>}
                  </div>
                </div>
                <Button size="sm" variant="outline" disabled={busy === g.id}
                  className="h-8 rounded-lg text-[11px] font-bold text-red-500 border-red-200 hover:bg-red-50 gap-1 shrink-0"
                  onClick={() => revoke(g)}>
                  {busy === g.id ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} />} 解除
                </Button>
              </div>
            ))}
          </div>
        )}
      </CardContent>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
          onClick={() => !saving && setOpen(false)}>
          <div className="w-[480px] max-w-[92vw] rounded-3xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-lg font-black text-slate-900 flex items-center gap-2">
                  <Database size={18} className="text-blue-600" /> {connectionName}
                </h3>
                <p className="text-xs text-slate-400 mt-0.5">
                  {restricted ? '把该连接分配给一位用户' : '分配后该连接将转为白名单制,未被分配的人会失去访问'}
                </p>
              </div>
              <button onClick={() => !saving && setOpen(false)} className="text-slate-300 hover:text-slate-500"><X size={18} /></button>
            </div>

            <div className="mt-4 flex gap-2">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
                <Input value={keyword} onChange={(e) => setKeyword(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && searchUsers()}
                  placeholder="搜索用户(姓名/账号/手机号)..." className="pl-9 h-9 rounded-xl text-sm" />
              </div>
              <Button size="sm" className="h-9 rounded-xl bg-blue-600 hover:bg-blue-700 text-xs font-bold px-3"
                disabled={searching} onClick={searchUsers}>
                {searching ? <Loader2 size={13} className="animate-spin" /> : '搜索'}
              </Button>
            </div>

            <div className="mt-3 space-y-1.5 max-h-[36vh] overflow-y-auto pr-1 custom-scrollbar">
              {candidates === null ? null : candidates.length === 0 ? (
                <div className="text-center py-8 text-xs text-slate-300">未找到用户</div>
              ) : candidates.map((u: any) => {
                const already = grants.some((g) => g.user_id === u.id);
                return (
                  <button key={u.id} onClick={() => setPicked(u.id)} disabled={already}
                    className={cn('w-full text-left rounded-xl border p-3 transition-all flex items-center gap-3',
                      already ? 'border-slate-100 bg-slate-50 opacity-60 cursor-not-allowed'
                        : picked === u.id ? 'border-blue-500 bg-blue-50/70' : 'border-slate-100 hover:border-blue-200')}>
                    <div className="h-8 w-8 rounded-lg bg-blue-100 text-blue-700 flex items-center justify-center font-black text-xs shrink-0">
                      {(u.name || u.account || '?')[0]}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-bold text-slate-800 truncate">{u.name || u.account}</div>
                      <div className="text-[10px] text-slate-400 font-mono truncate">
                        {u.account}{u.role ? ` · ${ROLE_LABEL[u.role] || u.role}` : ''}
                      </div>
                    </div>
                    {already && <span className="text-[10px] text-slate-400 shrink-0">已分配</span>}
                  </button>
                );
              })}
            </div>

            <textarea rows={2} value={note}
              onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setNote(e.target.value)}
              placeholder="备注(可选):如「票务组,只看本景区看板」"
              className="mt-3 w-full rounded-xl text-sm border border-slate-200 bg-slate-50 px-3 py-2 outline-none focus:border-blue-400 resize-none" />

            <div className="flex justify-end gap-2 mt-4">
              <Button variant="outline" size="sm" className="rounded-xl text-xs font-bold" disabled={saving}
                onClick={() => setOpen(false)}>取消</Button>
              <Button size="sm" className="rounded-xl text-xs font-bold bg-blue-600 hover:bg-blue-700 text-white gap-1.5"
                disabled={saving || !picked} onClick={submit}>
                {saving ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />} 确认分配
              </Button>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
};

export default QbiGrantPanel;
