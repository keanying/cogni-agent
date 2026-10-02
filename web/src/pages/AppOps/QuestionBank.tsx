import React, { useEffect, useMemo, useState } from 'react';
import { Plus, Search, Loader2, Pencil, Trash2, Zap, Bot, RefreshCw, AlertTriangle } from 'lucide-react';
import { Card } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { CopyId, Modal, ConfirmModal } from '../../components/ui/PageHelpers';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { canDeleteRow, canEditRow, useCapabilities, whyDisabled } from '../../lib/capabilities';
import DimensionLabelPicker from '../Tools/QuickBI/DimensionLabelPicker';
import { ScenicSelect, inputCls, labelCls, selectCls } from '../Tools/QuickBI/shared';

/**
 * 快捷问题 = 自研智能体的固定问题。
 *
 * 以前这一页读的是 app_question_bank（早期的 APP 问题库），而 0076/0079 之后
 * 真正生效的是统一问题表 scenic_agent_issue_detail —— 前台的问题树、Quick BI 的固定问题
 * 都在那张表上。于是这一页永远是空的：运营在这儿建了问题，APP 上一条也看不到，
 * 而页面不会报任何错，只写着「暂无问题」。
 *
 * 现在与 Quick BI 固定问题同表同口径，差别只在 business_class：
 *   quick_bi     → 直连 Quick BI（在「工具中心 · Quick BI · 固定问题」维护）
 *   local_system → 走我们自己的对话引擎（就是这一页）
 */

const TIME_PERIODS: Record<string, string> = {
  '': '不限', today: '今日', yesterday: '昨天', week_to_date: '本周',
  last_7_days: '近7天', month_to_date: '本月', last_month: '上月',
};

const emptyDraft = (scenicId?: string | null): Partial<api.ScenicIssue> => ({
  business_class: 'local_system', title: '', issue: '',
  agent_id: null, skill_id: null, model_instruction: '',
  time_period: '', sort_order: 0, active: true,
  scenic_id: scenicId ?? null,
});

const QuestionBank: React.FC = () => {
  const [items, setItems] = useState<api.ScenicIssue[]>([]);
  const [labelTree, setLabelTree] = useState<api.DimensionLabelTopic[]>([]);
  const [skills, setSkills] = useState<any[]>([]);
  const [agents, setAgents] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [fTopic, setFTopic] = useState('');
  const [fScenic, setFScenic] = useState('');
  const [onlyUnlabeled, setOnlyUnlabeled] = useState(false);
  const [editing, setEditing] = useState<Partial<api.ScenicIssue> | null>(null);
  const [deleting, setDeleting] = useState<api.ScenicIssue | null>(null);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState('');
  const cap = useCapabilities();

  const load = async () => {
    setLoading(true);
    try {
      const r = await api.listScenicIssues({
        business_class: 'local_system',
        scenic_id: fScenic || undefined,
        topic_id: fTopic || undefined,
        keyword: keyword || undefined,
      });
      setItems(r.items || []);
    } catch (e: any) { toast.error(e.message || '加载问题列表失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [fTopic, fScenic]);
  useEffect(() => {
    api.getDimensionLabelTree().then((r) => setLabelTree(r.topics)).catch(() => {});
    api.listSkills().then((r) => setSkills(r.items || [])).catch(() => {});
    api.listAgents?.().then((r: any) => setAgents(r.items || [])).catch(() => {});
  }, []);

  // 景区名：列表里显示中文名而不是一串 PFTSC… 编号
  const [scenicNames, setScenicNames] = useState<Record<string, string>>({});
  useEffect(() => {
    const ids = Array.from(new Set(items.map((i) => i.scenic_id).filter(Boolean))) as string[];
    const missing = ids.filter((id) => !scenicNames[id]);
    if (!missing.length) return;
    Promise.all(missing.map((id) => api.getScenicCatalogDetail(id)
      .then((r) => [id, r.scenic_name] as const).catch(() => [id, id] as const)))
      .then((pairs) => setScenicNames((prev) => ({ ...prev, ...Object.fromEntries(pairs) })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  const filtered = useMemo(() => items.filter((q) => {
    if (onlyUnlabeled && q.metric_id) return false;
    if (keyword && !(q.title + q.issue).includes(keyword)) return false;
    return true;
  }), [items, keyword, onlyUnlabeled]);

  const targetName = (q: Partial<api.ScenicIssue>) => {
    if (q.skill_id) return skills.find((s) => s.id === q.skill_id)?.name || `技能 ${String(q.skill_id).slice(0, 8)}`;
    if (q.agent_id) return agents.find((a) => a.id === q.agent_id)?.name || `Agent ${String(q.agent_id).slice(0, 8)}`;
    return '';
  };

  const save = async () => {
    if (!editing) return;
    const title = (editing.title || '').trim();
    const issue = (editing.issue || '').trim();
    if (!issue) { toast.error('请填写问题原文'); return; }
    setSaving(true);
    try {
      const payload: Partial<api.ScenicIssue> = {
        business_class: 'local_system',
        title: title || issue.slice(0, 40),
        issue,
        topic_id: editing.topic_id || null,
        dimension_id: editing.dimension_id || null,
        metric_id: editing.metric_id || null,
        agent_id: editing.agent_id || null,
        skill_id: editing.skill_id || null,
        model_instruction: editing.model_instruction || null,
        time_period: editing.time_period || null,
        scenic_id: editing.scenic_id || null,
        sort_order: Number(editing.sort_order) || 0,
        active: editing.active !== false,
      };
      if (editing.issue_id) await api.updateScenicIssue(editing.issue_id, payload);
      else await api.createScenicIssue(payload);
      toast.success(editing.issue_id ? '已更新' : '已创建');
      setEditing(null); load();
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSaving(false); }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setDeletingId(deleting.issue_id);
    try {
      await api.deleteScenicIssue(deleting.issue_id);
      toast.success('已删除');
      setDeleting(null); load();
    } catch (e: any) { toast.error(e.message || '删除失败'); }
    finally { setDeletingId(''); }
  };

  const topics = useMemo(() => labelTree.map((t) => ({ id: t.topic_id, name: t.topic })), [labelTree]);

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">快捷问题</h1>
          <p className="text-slate-500 text-sm mt-1">
            自研智能体的固定问题，与 Quick BI 固定问题<b>同一张表</b>，在 APP 的问题树上并排展示。
            绑定技能/Agent 后命中即直达；不绑就走意图路由。
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={load} className="h-11 rounded-xl gap-1.5">
            <RefreshCw size={15} />刷新
          </Button>
          <Button onClick={() => setEditing(emptyDraft(
            cap.manage_all || cap.is_group ? null : cap.scope_scenic_id))}
            disabled={!cap.can_create} title={cap.can_create ? undefined : whyDisabled(cap, null, '修改')}
            className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl gap-2 shadow-lg shadow-blue-200 h-11 px-6">
            <Plus size={18} />新建问题
          </Button>
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm bg-white/50 backdrop-blur-sm p-4 space-y-3">
        <div className="flex flex-col md:flex-row gap-3">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
            <Input value={keyword} onChange={(e) => setKeyword(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && load()}
              placeholder="搜索标题 / 问题原文，回车搜索..."
              className="pl-10 h-11 bg-white border-slate-200 rounded-xl" />
          </div>
          <div className="w-full md:w-56">
            <ScenicSelect registeredOnly placeholder="全部景区" value={fScenic} onChange={setFScenic} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {['', ...topics.map((t) => t.id)].map((t) => (
            <button key={t || 'all'} onClick={() => setFTopic(t)}
              className={cn('px-3 py-1.5 rounded-lg text-xs font-bold transition-all',
                fTopic === t ? 'bg-blue-600 text-white'
                  : 'bg-white border border-slate-200 text-slate-500 hover:text-slate-700')}>
              {t ? topics.find((x) => x.id === t)?.name : '全部主题'}
            </button>
          ))}
          <label className="ml-2 flex items-center gap-1.5 text-xs text-slate-600">
            <input type="checkbox" checked={onlyUnlabeled}
              onChange={(e) => setOnlyUnlabeled(e.target.checked)} />
            只看未挂标签
          </label>
        </div>
      </Card>

      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs text-slate-500">
            <tr>
              <th className="px-4 py-2.5">问题</th>
              <th className="px-4 py-2.5">主题 / 维度 / 指标</th>
              <th className="px-4 py-2.5">执行目标</th>
              <th className="px-4 py-2.5">时间口径</th>
              <th className="px-4 py-2.5">最近运行</th>
              <th className="px-4 py-2.5 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={6} className="py-16 text-center">
                <Loader2 className="mx-auto animate-spin text-slate-300" size={26} /></td></tr>
            )}
            {!loading && filtered.length === 0 && (
              <tr><td colSpan={6} className="py-16 text-center text-sm text-slate-400">
                暂无快捷问题，点击右上角新建。
              </td></tr>
            )}
            {!loading && filtered.map((q) => (
              <tr key={q.issue_id} className={cn('border-t border-slate-100', !q.active && 'opacity-50')}>
                <td className="max-w-md px-4 py-3">
                  <div className="font-medium text-slate-800">{q.title}</div>
                  <div className="truncate text-xs text-slate-400">{q.issue}</div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-slate-300">
                    <CopyId id={q.issue_id} />
                    <span>{q.scenic_id ? (scenicNames[q.scenic_id] || q.scenic_id) : '集团级'}</span>
                  </div>
                </td>
                <td className="px-4 py-3">
                  {q.metric_id ? (
                    <div className="text-xs text-slate-600">
                      {q.topic} / {q.dimension}
                      <div className="font-semibold text-slate-700">{q.metric}</div>
                    </div>
                  ) : (
                    // 三级标签是 APP 问题树的唯一挂载点：没挂的问题前台一条都看不到，
                    // 而后台列表看起来一切正常 —— 这是「配了但前台没有」最常见的一种
                    <span className="inline-flex items-center gap-1 rounded bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-600">
                      <AlertTriangle size={11} />未挂标签 · APP 不展示
                    </span>
                  )}
                </td>
                <td className="px-4 py-3">
                  {q.skill_id ? (
                    <Badge className="border border-violet-200 bg-violet-50 text-violet-600 gap-1">
                      <Zap size={11} />{targetName(q)}
                    </Badge>
                  ) : q.agent_id ? (
                    <Badge className="border border-blue-200 bg-blue-50 text-blue-600 gap-1">
                      <Bot size={11} />{targetName(q)}
                    </Badge>
                  ) : (
                    <span className="text-xs text-slate-400">意图路由</span>
                  )}
                </td>
                <td className="px-4 py-3 text-xs text-slate-500">
                  {TIME_PERIODS[q.time_period || ''] || q.time_period || '—'}
                </td>
                <td className="px-4 py-3 text-xs">
                  {q.last_run_status ? (
                    <span className={cn(q.last_run_status === 'success' ? 'text-emerald-600' : 'text-rose-500')}>
                      {q.last_run_status === 'success' ? '成功' : '失败'}
                    </span>
                  ) : <span className="text-slate-300">—</span>}
                  {q.last_run_time && (
                    <div className="text-[10px] text-slate-300">
                      {new Date(q.last_run_time).toLocaleString()}
                    </div>
                  )}
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-right">
                  <Button size="sm" variant="ghost" disabled={!canEditRow(cap, q.scenic_id)}
                    title={canEditRow(cap, q.scenic_id) ? '编辑' : whyDisabled(cap, q.scenic_id, '修改')}
                    onClick={() => setEditing({ ...q })}><Pencil size={13} /></Button>
                  <Button size="sm" variant="ghost" className="text-rose-500"
                    disabled={!canDeleteRow(cap, q.scenic_id) || deletingId === q.issue_id}
                    title={canDeleteRow(cap, q.scenic_id) ? '删除' : whyDisabled(cap, q.scenic_id, '删除')}
                    onClick={() => setDeleting(q)}>
                    {deletingId === q.issue_id ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editing && (
        <Modal title={editing.issue_id ? '编辑快捷问题' : '新建快捷问题'} wide onClose={() => setEditing(null)}
          footer={<>
            <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
            <Button className="bg-[#2563eb]" disabled={saving || !(editing.issue || '').trim()} onClick={save}>
              {saving && <Loader2 size={14} className="mr-1 animate-spin" />}保存
            </Button>
          </>}>
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2">
              <label className={labelCls}>标题</label>
              <input className={inputCls} value={editing.title || ''}
                onChange={(e) => setEditing({ ...editing, title: e.target.value })}
                placeholder="列表与 APP 上显示的短文案；不填默认取问题前 40 字" />
            </div>
            <div className="col-span-2">
              <label className={labelCls}>问题原文 *</label>
              <textarea className="min-h-[72px] w-full rounded-lg border border-slate-200 p-2 text-sm outline-none focus:border-blue-400"
                value={editing.issue || ''}
                onChange={(e) => setEditing({ ...editing, issue: e.target.value })}
                placeholder="真正发给智能体的那句话，如：今天景区在园人数多少" />
            </div>
            <div className="col-span-2">
              <label className={labelCls}>口径说明（发给模型的约束，不展示给用户）</label>
              <textarea className={cn(inputCls, 'h-20 py-2')} value={editing.model_instruction || ''}
                onChange={(e) => setEditing({ ...editing, model_instruction: e.target.value })}
                placeholder="如：只看今天 00:00 至此刻，先给结论再给数据，超过日承载量 80% 要提示预警" />
              <p className="mt-1 text-[11px] text-slate-400">
                按钮上的文案要短，而模型需要的恰恰是那些啰嗦的约束 —— 发起时它会<b>追加</b>在问题后面，不替换用户原话。
              </p>
            </div>
            <div>
              <label className={labelCls}>执行技能（优先于 Agent）</label>
              <select className={selectCls} value={editing.skill_id || ''}
                onChange={(e) => setEditing({ ...editing, skill_id: e.target.value || null })}>
                <option value="">不指定</option>
                {skills.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </div>
            <div>
              <label className={labelCls}>执行 Agent</label>
              <select className={selectCls} value={editing.agent_id || ''}
                onChange={(e) => setEditing({ ...editing, agent_id: e.target.value || null })}>
                <option value="">不指定</option>
                {agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              <p className="mt-1 text-[11px] text-slate-400">两个都不选 = 走意图路由挑默认 Agent。</p>
            </div>
            <div className="col-span-2">
              <label className={labelCls}>主题 / 维度 / 指标（APP 问题树的挂载点）</label>
              <DimensionLabelPicker tree={labelTree} value={editing as any}
                onChange={(v) => setEditing({ ...editing, ...v })} />
              <p className="mt-1 text-[11px] text-slate-400">
                三级都不选也能存（后台仍可用），但<b>前台看不到</b> —— 问题树只认三级标签。
              </p>
            </div>
            <div>
              <label className={labelCls}>时间口径</label>
              <select className={selectCls} value={editing.time_period || ''}
                onChange={(e) => setEditing({ ...editing, time_period: e.target.value || null })}>
                {Object.entries(TIME_PERIODS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </div>
            <div>
              <label className={labelCls}>排序</label>
              <input type="number" className={inputCls} value={editing.sort_order ?? 0}
                onChange={(e) => setEditing({ ...editing, sort_order: Number(e.target.value) })} />
            </div>
            <div className="col-span-2">
              <label className={labelCls}>归属景区（留空 = 集团级，全景区通用）</label>
              <ScenicSelect value={editing.scenic_id || ''} registeredOnly
                disabled={!cap.manage_all && !cap.is_group}
                onChange={(v) => setEditing({ ...editing, scenic_id: v || null })} />
              {!cap.manage_all && !cap.is_group && (
                <p className="mt-1 text-[11px] text-slate-400">
                  景区角色只能维护本景区（{cap.scope_scenic_id || '未绑定'}）的问题
                </p>
              )}
            </div>
            <label className="col-span-2 flex items-center gap-2 text-xs text-slate-600">
              <input type="checkbox" checked={editing.active !== false}
                onChange={(e) => setEditing({ ...editing, active: e.target.checked })} />启用
            </label>
          </div>
        </Modal>
      )}

      {deleting && (
        <ConfirmModal title="删除快捷问题" danger confirmLabel="删除"
          message={`确定删除「${deleting.title}」？删除后 APP 的问题树上也会消失。`}
          onCancel={() => setDeleting(null)} onConfirm={confirmDelete} />
      )}
    </div>
  );
};

export default QuestionBank;
