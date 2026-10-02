import React, { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Flame, Loader2, Pencil, RefreshCw, Sparkles, XCircle } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import { ScenicSelect, inputCls, labelCls } from './shared';
import { Modal } from '../../../components/ui/PageHelpers';
import { canEditRow, useCapabilities, whyDisabled } from '../../../lib/capabilities';

/**
 * 主题解读配置面板。
 *
 * APP 首页那 5 张主题卡片(概览/客群/票务/舆情/OTA直销)点下去应该出一份该主题的解读报告。
 * 能不能出,取决于这 5 条问题有没有绑 Quick BI —— 没绑的话点下去走通用对话,
 * 回一句「请告诉我您需要查询什么」,而用户点的明明是「客群解读」。
 * 这个面板就是把这件事显式化:哪个景区、绑了没有、绑的是哪条连接、打在哪份数据上。
 *
 * 界面上的两个取舍:
 * - **默认落在自己的景区**,不是集团级。主题解读按景区配,集团级通常一条连接都没绑,
 *   默认停在那儿一进来就是 5 条全红 + 「没有可用连接」,看着像坏了。
 * - **配好了就收起来**。这是装配期的面板,5/5 之后它只是一行绿字;摊开占着半屏,
 *   下面的固定问题列表反而被挤下去 —— 那才是每天要看的东西。
 */
const ThemeIssuePanel: React.FC = () => {
  const [scenic, setScenic] = useState('');
  const [items, setItems] = useState<api.ThemeIssueStatus[]>([]);
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [connectionName, setConnectionName] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(true);
  const cap = useCapabilities();
  // 生成 = 往这个景区下写 5 条问题,所以按「能不能改这个景区的内容」判定;
  // scenic 为空是集团级,只有集团三角色能写
  const canWrite = canEditRow(cap, scenic || null) || (!scenic && cap.manage_all);
  const writeHint = canWrite ? undefined : whyDisabled(cap, scenic || null, '修改');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.getThemeIssues(scenic || undefined);
      setItems(r.items);
      setConnectionId(r.connection_id);
      setConnectionName(r.connection_name);
    } catch (e: any) { toast.error(e.message || '读取失败'); }
    finally { setLoading(false); }
  }, [scenic]);
  useEffect(() => { load(); }, [load]);

  // 默认落在「自己的景区」:
  //   景区账号 → 它绑的那个景区(它也只能配这一个);
  //   集团账号 → 没有「自己的景区」,取第一个已登记的,让面板一打开就是能用的。
  //             集团级仍然可选(要给所有景区共用一份时才选它)。
  const [pickedDefault, setPickedDefault] = useState(false);
  useEffect(() => {
    if (pickedDefault || scenic || !cap.role) return;
    let alive = true;
    // 由后端挑:它知道哪个景区有可用连接、哪个已经配过主题解读。
    // 前端按「第一个已登记的」挑的话,十有八九挑到一个还没绑连接的景区 ——
    // 一进来就是 5 条全红,看着像坏了,其实只是选错了范围。
    api.getThemeDefaultScenic()
      .then((r) => { if (alive && r.scenic_id) setScenic(r.scenic_id); })
      .catch(() => {
        // 退一步:景区账号至少能落在自己景区上
        if (alive && !cap.is_group && cap.scope_scenic_id) setScenic(cap.scope_scenic_id);
      })
      .finally(() => { if (alive) setPickedDefault(true); });
    return () => { alive = false; };
  }, [cap.role, cap.is_group, cap.scope_scenic_id, scenic, pickedDefault]);

  const generate = async (overwrite: boolean) => {
    setBusy(true);
    try {
      const r = await api.generateThemeIssues({ scenic_id: scenic || undefined, overwrite });
      toast.success(`已生成 ${r.created.length} 条,更新 ${r.updated.length} 条,跳过 ${r.skipped.length} 条`);
      load();
    } catch (e: any) { toast.error(e.message || '生成失败'); }
    finally { setBusy(false); }
  };

  // 立即预热:报告一条要 3~10 分钟,所以后端只负责把任务起起来、立刻返回。
  // 这里不转圈等 —— 等的话浏览器早超时了,而报告其实还在跑,看着像失败。
  const prewarm = async () => {
    setBusy(true);
    try {
      const r = await api.prewarmThemeIssues({ scenic_id: scenic || undefined });
      toast.success(r.message);
      setTimeout(load, 1500);
    } catch (e: any) { toast.error(e.message || '预热失败'); }
    finally { setBusy(false); }
  };

  // 提示词编辑:主题解读那五条的口径原先写死在代码里,改不了 ——
  // 而口径正是报告的成败所在:写得含糊,报告就只会泛泛而谈。
  const [editing, setEditing] = useState<{ item: api.ThemeIssueStatus; text: string } | null>(null);
  const [savingPrompt, setSavingPrompt] = useState(false);

  // 提示词随列表一起回来了,不必再单独查一次
  const openPrompt = (i: api.ThemeIssueStatus) => {
    if (!i.issue_id) return;
    setEditing({ item: i, text: i.model_instruction || '' });
  };

  const savePrompt = async () => {
    if (!editing?.item.issue_id) return;
    setSavingPrompt(true);
    try {
      await api.qbiSaveFixed({ model_instruction: editing.text }, editing.item.issue_id);
      toast.success('提示词已保存,下次生成报告就按它来');
      setEditing(null);
      load();
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSavingPrompt(false); }
  };

  const ready = items.filter((i) => i.ready).length;
  const allReady = items.length > 0 && ready === items.length;
  // 配好了就默认收起 —— 一条绿字足够,不必占半屏
  useEffect(() => { if (allReady) setOpen(false); }, [allReady]);

  return (
    <div className={cn('rounded-2xl border bg-white',
                       allReady ? 'border-slate-200' : 'border-amber-200 bg-amber-50/30')}>
      {/* 标题行:永远可见,一眼看到「几个主题配好了」 */}
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-3 text-left">
        {open ? <ChevronDown size={15} className="text-slate-400" /> : <ChevronRight size={15} className="text-slate-400" />}
        <Sparkles size={15} className={allReady ? 'text-emerald-500' : 'text-amber-500'} />
        <span className="text-sm font-bold text-slate-800">主题解读(APP 首页 5 张卡片)</span>
        <span className={cn('rounded-full px-2 py-0.5 text-[11px] font-bold',
                            allReady ? 'bg-emerald-50 text-emerald-600' : 'bg-amber-100 text-amber-700')}>
          {ready} / {items.length || 5} 已绑定
        </span>
        {!allReady && !loading && (
          <span className="text-[11px] text-amber-700">未绑定的主题,前台点卡片会走通用对话、答非所问</span>
        )}
        <span className="flex-1" />
        {loading && <Loader2 size={13} className="animate-spin text-slate-300" />}
      </button>

      {open && (
        <div className="border-t border-slate-100 px-4 pb-4 pt-3">
          <div className="mb-3 grid grid-cols-1 gap-2 md:grid-cols-2">
            <div>
              <label className={labelCls}>给哪个景区配</label>
              <ScenicSelect value={scenic} onChange={setScenic} placeholder="集团级(所有景区共用)" registeredOnly
                disabled={!cap.manage_all && !cap.is_group} />
              <p className="mt-1 text-[11px] text-slate-400">
                {cap.is_group
                  ? '留空 = 集团级,所有还没单独配的景区共用这一份'
                  : `只能配本景区(${cap.scope_scenic_id || '未绑定'})`}
              </p>
            </div>
            <div>
              <label className={labelCls}>该景区当前可用的 Quick BI 连接</label>
              <div className={cn('flex h-10 items-center rounded-lg border px-3 text-sm',
                                 connectionId ? 'border-slate-200 text-slate-700'
                                              : 'border-amber-300 bg-amber-50 text-amber-800')}>
                {connectionId
                  ? <span className="truncate font-medium">{connectionName || connectionId}</span>
                  : <><AlertTriangle size={13} className="mr-1.5 shrink-0" />
                      没有可用连接 —— 先去「连接配置」把连接的归属景区绑到这个景区</>}
              </div>
            </div>
          </div>

          {/* 连接都没有的时候,不必把 5 行红字摊开:先把该做的那一步说清楚 */}
          {!connectionId ? (
            <div className="rounded-xl border border-dashed border-slate-200 px-4 py-6 text-center">
              <p className="text-sm text-slate-500">这个景区还没有能用的 Quick BI 连接,主题解读没法绑。</p>
              <p className="mt-1 text-[11px] text-slate-400">
                去上方「连接配置」新建一条并把归属景区选成这个景区;或建一条集团级连接,供所有景区共用。
              </p>
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-slate-100">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs text-slate-500">
                  <tr><th className="px-3 py-2">主题</th><th className="px-3 py-2">状态</th>
                    <th className="px-3 py-2">归属</th><th className="px-3 py-2">目标数据</th>
                    <th className="px-3 py-2">最近运行</th><th className="px-3 py-2 text-right">提示词</th></tr>
                </thead>
                <tbody>
                  {loading && <tr><td colSpan={6} className="py-8 text-center"><Loader2 className="mx-auto animate-spin text-slate-300" /></td></tr>}
                  {!loading && items.map((i) => (
                    <tr key={i.theme_code} className="border-t border-slate-100">
                      <td className="px-3 py-2 font-medium text-slate-700">{i.theme}</td>
                      <td className="px-3 py-2">
                        {i.ready
                          ? <span className="inline-flex items-center gap-1 text-xs text-emerald-600"><CheckCircle2 size={13} />已绑 Quick BI</span>
                          : <span className="inline-flex items-center gap-1 text-xs text-rose-500"><XCircle size={13} />未配置 · 前台会走通用对话</span>}
                      </td>
                      <td className="px-3 py-2 text-xs text-slate-500">{i.scope}</td>
                      <td className="px-3 py-2 text-xs text-slate-500">{i.target_data_name || '智能选表'}</td>
                      <td className="px-3 py-2 text-xs">
                        {i.last_run_status
                          ? <span className={cn(i.last_run_status === 'success' ? 'text-emerald-600'
                                               : i.last_run_status === 'failed' ? 'text-rose-500' : 'text-blue-500')}>
                              {({ success: '成功', failed: '失败', running: '生成中' } as any)[i.last_run_status] || i.last_run_status}
                              {i.last_run_at && <span className="ml-1 text-slate-300">
                                {new Date(i.last_run_at).toLocaleString()}</span>}
                            </span>
                          : <span className="text-slate-300">从未运行</span>}
                      </td>
                      <td className="px-3 py-2 text-right">
                        {/* 提示词就地改:主题解读的口径原先写死在代码里,改不了。
                            这五条是报告的成败所在 —— 口径写得含糊,报告就泛泛而谈。 */}
                        <Button size="sm" variant="ghost" className="gap-1"
                          disabled={!i.issue_id || !canWrite}
                          title={writeHint || '编辑这个主题的报告提示词'}
                          onClick={() => openPrompt(i)}>
                          <Pencil size={12} />编辑
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button size="sm" className="gap-1 bg-[#2563eb]" disabled={busy || !canWrite || !connectionId}
              title={writeHint || (connectionId ? undefined : '先给这个景区配一条可用连接')}
              onClick={() => generate(false)}>
              {busy ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />}生成缺失的
            </Button>
            <Button variant="outline" size="sm" disabled={busy || !canWrite || !connectionId}
              onClick={() => generate(true)}
              title={writeHint || '按当前连接与数据把 5 条全部重绑一遍'}>全部重绑</Button>
            <Button variant="outline" size="sm" className="gap-1" disabled={busy || !canWrite || !connectionId}
              title={writeHint || '现在就把还没跑过的报告跑一遍(不等它跑完)'}
              onClick={prewarm}>
              <Flame size={13} />立即预热
            </Button>
            <Button variant="outline" size="sm" className="gap-1" onClick={load} disabled={loading}>
              <RefreshCw size={13} />刷新
            </Button>
            <span className="flex-1" />
            <span className="text-[11px] text-slate-400">
              解读是报告类的重活,12 小时内重复点同一主题会直接回放上一次结果(预热直出)。
            </span>
          </div>
        </div>
      )}

      {editing && (
        <Modal title={`「${editing.item.theme}」报告提示词`} wide onClose={() => setEditing(null)}
          footer={<>
            <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
            <Button className="bg-[#2563eb]" disabled={savingPrompt} onClick={savePrompt}>
              {savingPrompt && <Loader2 size={14} className="mr-1 animate-spin" />}保存
            </Button>
          </>}>
          <div className="space-y-2">
            <p className="text-[11px] text-slate-500">
              这段话会跟着问题一起发给模型,决定报告怎么写:读看板上的哪几张图、
              按什么顺序说、要不要带同比环比、给几条建议、写多长。
              <b className="text-slate-700">改完不会被「全部重绑」冲掉</b> ——
              重绑只重绑连接与目标数据。
            </p>
            <textarea className={cn(inputCls, 'h-64 py-2 font-mono text-[12px] leading-relaxed')}
              value={editing.text} onChange={(e) => setEditing({ ...editing, text: e.target.value })}
              placeholder="例:严格基于看板图表数据写解读,每个结论带上来源图表名与关键数值,有同比环比必须写出来,最后给 2-3 条可执行建议,全文 600-900 字" />
            <p className="text-[11px] text-slate-400">
              留空 = 只把问题原文发过去。报告类尤其不建议留空:问题只有「【主题解读】{editing.item.theme}」
              这几个字,模型只能靠猜。
            </p>
          </div>
        </Modal>
      )}
    </div>
  );
};

export default ThemeIssuePanel;
