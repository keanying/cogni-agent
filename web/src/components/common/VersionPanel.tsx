import React, { useEffect, useState } from 'react';
import {
  GitBranch, Loader2, Rocket, PowerOff, Undo2, AlertTriangle, CheckCircle2, FileEdit, Copy,
  ShieldCheck, Lock,
} from 'lucide-react';
import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { confirmDialog } from '../ui/confirm';

/**
 * 版本管理面板。Tool / Skill / Agent 共用同一个组件 ——
 * 后端是一套统一接口,前端也没必要写三遍。
 *
 * 展示上刻意强调**状态语义**而不只是罗列版本号:哪个在跑、哪个是草稿、下线会发生什么。
 * 版本管理最容易出的事故是"以为下线了其实换了一套逻辑在跑",所以这些后果都写在按钮旁边。
 */

const STATUS_META: Record<api.VersionStatus, { label: string; cls: string; desc: string }> = {
  published: { label: '生效中', cls: 'border-emerald-100 bg-emerald-50 text-emerald-700',
               desc: '执行引擎实际使用的就是这个版本' },
  draft:     { label: '草稿',   cls: 'border-amber-100 bg-amber-50 text-amber-700',
               desc: '尚未生效,可随意修改' },
  offline:   { label: '已下线', cls: 'bg-slate-100 text-slate-500',
               desc: '保留用于回滚' },
};

interface Props {
  resourceType: 'tool' | 'skill' | 'agent';
  resourceId: string;
  /** 版本切换后通知外层刷新主体表单(主表已被快照覆盖) */
  onChanged?: () => void;
  /** 值变化时强制重新加载版本列表(例如外层刚保存了草稿) */
  refreshToken?: number;
}

const VersionPanel: React.FC<Props> = ({ resourceType, resourceId, onChanged, refreshToken }) => {
  const [items, setItems] = useState<api.ResourceVersion[]>([]);
  const [available, setAvailable] = useState(true);
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [diffWith, setDiffWith] = useState<api.ResourceVersion | null>(null);
  // 发布预检(推荐方案):发布前先 dry-run,展示依赖闭包 + 拦截问题,确认后才真正发布
  const [publishTarget, setPublishTarget] = useState<api.ResourceVersion | null>(null);
  const [precheck, setPrecheck] = useState<api.PrecheckResult | null>(null);
  const [publishing, setPublishing] = useState(false);

  /** 点「发布」-> 先跑预检(dry-run),不改变任何状态 */
  const openPublishDialog = async (v: api.ResourceVersion) => {
    setBusy(v.id);
    try {
      const pc = await api.precheckResourceVersion(v.id);
      setPrecheck(pc);
      setPublishTarget(v);
    } catch (e: any) {
      toast.error(e.message || '发布预检失败');
    } finally { setBusy(null); }
  };

  /** 确认发布。15 号文档:发布不设默认、不隐含对任何人生效;
   *  零 user 指派时后端返回 hint(黄条)提醒去分发中心按用户指派。 */
  const doPublish = async () => {
    if (!publishTarget) return;
    setPublishing(true);
    try {
      const r = await api.publishResourceVersion(publishTarget.id);
      toast.success(r.message || `v${r.version} 已发布`);
      if (r.hint) toast.warning(r.hint, { duration: 10000 });
      setPublishTarget(null);
      setPrecheck(null);
      await load();
      onChanged?.();
    } catch (e: any) {
      toast.error(e.message || '发布失败');
    } finally { setPublishing(false); }
  };

  const load = async () => {
    setLoading(true);
    try {
      const r = await api.listResourceVersions(resourceType, resourceId);
      setItems(r.items);
      setAvailable(r.available);
      setReason(r.unavailable_reason);
    } catch (e: any) {
      toast.error(e.message || '加载版本失败');
    } finally { setLoading(false); }
  };
  useEffect(() => { if (resourceId) load(); }, [resourceType, resourceId, refreshToken]);

  const act = async (id: string, fn: () => Promise<any>, okMsg?: string) => {
    setBusy(id);
    try {
      const r = await fn();
      // 后端返回的 message / hint 往往说明了「接下来会发生什么」,原样透出比自己编一句好
      toast.success(r?.message || okMsg || '已完成', { duration: r?.hint ? 8000 : 4000 });
      if (r?.hint) toast.warning(r.hint, { duration: 10000 });
      await load();
      onChanged?.();
    } catch (e: any) {
      toast.error(e.message || '操作失败');
    } finally { setBusy(null); }
  };

  const snapshotCurrent = () =>
    act('new', () => api.createResourceVersion(resourceType, resourceId,
      { note: '基于当前配置创建的草稿' }), '已创建草稿版本');

  if (loading) {
    return <div className="flex justify-center py-10"><Loader2 className="animate-spin text-slate-300" size={20} /></div>;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <GitBranch size={15} className="text-slate-500" />
          <span className="text-sm font-bold text-slate-700">版本历史</span>
          <span className="text-[11px] text-slate-400">共 {items.length} 个</span>
        </div>
        <Button size="sm" variant="outline" className="h-8 gap-1.5 text-xs"
                disabled={busy === 'new'} onClick={snapshotCurrent}>
          {busy === 'new' ? <Loader2 size={12} className="animate-spin" /> : <Copy size={12} />}
          基于当前配置建草稿
        </Button>
      </div>

      {!available && (
        <div className="flex items-start gap-2 rounded-xl border border-rose-100 bg-rose-50 p-3 text-[12px] leading-relaxed text-rose-700">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{reason}</span>
        </div>
      )}

      {items.length === 0 ? (
        <div className="rounded-xl border-2 border-dashed border-slate-100 py-8 text-center text-xs text-slate-400">
          <p>还没有版本记录</p>
          <p className="mt-1 text-[11px] text-slate-300">
            版本化是后加的能力,没有记录时按当前配置执行。建一个草稿即可开始使用版本管理。
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((v) => {
            const meta = STATUS_META[v.status];
            const isBusy = busy === v.id;
            return (
              <div key={v.id}
                className={cn('rounded-xl border p-3 transition-all',
                  v.status === 'published' ? 'border-emerald-200 bg-emerald-50/30' : 'border-slate-200')}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm font-bold text-slate-800">
                        {v.version != null ? `v${v.version}` : '草稿'}
                      </span>
                      <Badge variant="outline" className={cn('text-[10px] font-bold', meta.cls)}>{meta.label}</Badge>
                      <span className="text-[10px] text-slate-400">{meta.desc}</span>
                    </div>
                    {v.note && <p className="mt-1 text-[12px] text-slate-600">{v.note}</p>}
                    <div className="mt-1 flex gap-3 text-[10px] text-slate-400">
                      <span>创建 {new Date(v.created_at).toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>
                      {v.published_at && <span>发布 {new Date(v.published_at).toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>}
                    </div>
                    {v.status === 'published' && v.locked_deps?.deps && (
                      <div className="mt-1 flex items-center gap-1 text-[10px] text-slate-400">
                        <Lock size={10} />依赖已锁定 · {v.locked_deps.deps.length} 项
                        {v.locked_deps.deps.some((d) => d.version == null) && ' · 含主表引用'}
                      </div>
                    )}
                  </div>

                  <div className="flex shrink-0 items-center gap-1">
                    <button onClick={() => setDiffWith(diffWith?.id === v.id ? null : v)}
                            className="rounded px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100">
                      {diffWith?.id === v.id ? '收起' : '查看内容'}
                    </button>

                    {v.status === 'draft' && (
                      <Button size="sm" className="h-7 gap-1 bg-[#1677ff] px-2 text-[11px]" disabled={isBusy}
                              onClick={() => openPublishDialog(v)}>
                        {isBusy ? <Loader2 size={11} className="animate-spin" /> : <Rocket size={11} />}发布
                      </Button>
                    )}
                    {v.status === 'offline' && (
                      <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-[11px]" disabled={isBusy}
                              onClick={async () => {
                                if (!await confirmDialog({
                                  title: `回滚到 v${v.version}`,
                                  message: '当前生效版本会被下线,之后的调用一律走这个旧版本。',
                                  confirmLabel: '回滚',
                                })) return;
                                act(v.id, () => api.rollbackResourceVersion(v.id));
                              }}>
                        {isBusy ? <Loader2 size={11} className="animate-spin" /> : <Undo2 size={11} />}回滚到此版本
                      </Button>
                    )}
                    {v.status === 'published' && (
                      <Button size="sm" variant="ghost" className="h-7 gap-1 px-2 text-[11px] text-rose-600" disabled={isBusy}
                              onClick={async () => {
                                // 把后果说清楚再让人确认。这一步最容易出事故:
                                // 以为"下线"是回到上一版,实际是整个资源停用。
                                if (!await confirmDialog({
                                  title: `下线 v${v.version}`,
                                  message: '它是当前生效版本。下线后本资源将无法被调用,直到你发布另一个版本。\n'
                                    + '系统不会自动回退到旧版本 —— 那会让「下线」变成「悄悄换一套行为在跑」。',
                                  confirmLabel: '下线',
                                  danger: true,
                                })) return;
                                act(v.id, () => api.offlineResourceVersion(v.id));
                              }}>
                        {isBusy ? <Loader2 size={11} className="animate-spin" /> : <PowerOff size={11} />}下线
                      </Button>
                    )}
                  </div>
                </div>

                {diffWith?.id === v.id && (
                  <pre className="mt-2 max-h-64 overflow-auto rounded-lg bg-[#0d1117] p-3 font-mono text-[11px] leading-relaxed text-slate-200">
                    {JSON.stringify(v.snapshot, null, 2)}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}

      {publishTarget && precheck && (
        <div className="rounded-xl border border-blue-200 bg-white p-4 shadow-md space-y-3">
          <div className="flex items-center gap-2">
            <ShieldCheck size={15} className="text-blue-600" />
            <span className="text-sm font-bold text-slate-800">发布预检</span>
            {precheck.pass ? (
              <Badge variant="outline" className="text-[10px] font-bold border-emerald-100 bg-emerald-50 text-emerald-700">
                校验通过 · 将固化为 v{precheck.will_version}
              </Badge>
            ) : (
              <Badge variant="outline" className="text-[10px] font-bold border-rose-100 bg-rose-50 text-rose-700">
                存在 {precheck.problems.length} 个问题,发布将被拦截
              </Badge>
            )}
          </div>
          {precheck.deps.length > 0 && (
            <div className="space-y-1">
              <div className="text-[11px] font-bold text-slate-600">依赖闭包 · {precheck.deps.length} 项(发布时冻结)</div>
              {precheck.deps.map((d, i) => (
                <div key={i} className="flex items-center gap-2 rounded-lg bg-slate-50 px-2 py-1 text-[11px] text-slate-600">
                  <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-bold">{d.resource_type}</span>
                  <span className="font-mono">{d.name}</span>
                  <span className="ml-auto font-mono text-slate-400">
                    {d.version != null ? `v${d.version}` : d.status === 'main_table' ? '主表(已发布)' : d.status}
                  </span>
                </div>
              ))}
            </div>
          )}
          {precheck.problems.length > 0 && (
            <div className="rounded-lg border border-rose-100 bg-rose-50 p-2 text-[11px] text-rose-700">
              <div className="mb-1 font-bold">以下依赖不可用,发布将被拦截:</div>
              <ul className="list-disc space-y-0.5 pl-4">
                {precheck.problems.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            </div>
          )}
          {precheck.pass && resourceType === 'agent' && (
            <div className="rounded-lg border border-amber-100 bg-amber-50 p-2 text-[11px] leading-relaxed text-amber-700">
              15 号起<b>发布不再设默认、不会自动对任何人生效</b>。
              发布完成后请到「分发中心」按用户指派,否则该 Agent 对用户不可用(调用将报
              <code className="mx-1 rounded bg-amber-100 px-1 font-mono">agent_not_distributed</code>)。
            </div>
          )}
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="outline" className="h-8" onClick={() => { setPublishTarget(null); setPrecheck(null); }}>
              取消
            </Button>
            <Button size="sm" className="h-8 gap-1 bg-[#1677ff]" disabled={!precheck.pass || publishing}
                    onClick={doPublish}>
              {publishing ? <Loader2 size={12} className="animate-spin" /> : <Rocket size={12} />}
              确认发布
            </Button>
          </div>
        </div>
      )}

      <div className="rounded-xl border border-blue-100 bg-blue-50/40 p-3 text-[11px] leading-relaxed text-blue-800/80">
        <div className="mb-1 flex items-center gap-1.5 font-bold text-blue-900">
          <CheckCircle2 size={12} />版本规则
        </div>
        <ul className="space-y-0.5">
          <li>· 已无「默认版本」概念(15 号):发布<b>不会自动对任何人生效</b>,需到分发中心<b>按用户指派</b>,否则用户调用将报 agent_not_distributed</li>
          <li>· 草稿不占版本号:<b>发布时才分配 vN</b>,派生了又丢弃不会留下版本空洞</li>
          <li>· 发布前自动<b>预检依赖</b>:引用了草稿/已下线/不存在的 skill 或 tool 会被拦截</li>
          <li>· 发布即<b>锁定依赖</b>(skill/tool 版本固化),防止下游偷偷升级导致行为漂移</li>
          <li>· <b>生效中的版本不能直接改</b>——保存时会自动基于它创建新草稿,原版本继续在线</li>
          <li>· 下线生效版本 = 本资源整体停用,不会自动回退到旧版本</li>
          <li>· 没有任何版本记录时按当前配置执行(兼容版本化之前创建的资源)</li>
        </ul>
      </div>
    </div>
  );
};

export default VersionPanel;
