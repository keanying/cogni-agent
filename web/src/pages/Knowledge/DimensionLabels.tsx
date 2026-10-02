import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Layers, Loader2, Pencil, Plus, RefreshCw, Search, Trash2 } from 'lucide-react';
import { Card } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { ConfirmModal, Modal } from '../../components/ui/PageHelpers';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { canDeleteRow, canEditRow, useCapabilities, whyDisabled } from '../../lib/capabilities';

/**
 * 维度标签:问题体系的「主题 → 维度 → 指标」三级路径(表 public_problem_dimension_label)。
 *
 * APP 的问题树、快捷问题 / Quick BI 固定问题的「挂到哪个指标下」,都取自这里。
 * 标签是**全平台共用**的一套 —— 某个景区改了,所有景区的问题树跟着变,所以只有集团角色能改。
 *
 * 这一页最容易出事的地方是**编码**:给已有主题加一个指标时,手敲一遍主题编码,
 * 敲成 `visitorflow`(小写 f)就会凭空多出一个同名主题,前台问题树里出现两个「客流」,
 * 而且不报任何错。所以新增时主题/维度优先从已有的里选,名称与编码成对带出,不让人重敲。
 */

const NEW = '__new__';

type Draft = api.DimensionLabelDraft & { id?: number };

const emptyDraft = (): Draft => ({
  topic: '', topic_id: '', dimension: '', dimension_id: '', metric: '', metric_id: '',
  sort_order: 0, active: true,
});

// 编码只允许字母数字下划线,首字符字母 —— 前台按编码分组、拼路径,带空格或中文会出怪问题
const CODE_RE = /^[A-Za-z][A-Za-z0-9_]*$/;

const DimensionLabels: React.FC = () => {
  const cap = useCapabilities();
  // 标签是集团级内容(不归属任何景区):按「集团级行」判权,与后端 _assert_scope(user, None) 同口径
  const canWrite = canEditRow(cap, null);
  const canDel = canDeleteRow(cap, null);

  const [rows, setRows] = useState<api.DimensionLabel[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [topicFilter, setTopicFilter] = useState('');
  const [showInactive, setShowInactive] = useState(true);

  const [editing, setEditing] = useState<Draft | null>(null);
  const [original, setOriginal] = useState<api.DimensionLabel | null>(null);
  const [topicPick, setTopicPick] = useState('');
  const [dimPick, setDimPick] = useState('');
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<api.DimensionLabel | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const r = await api.getDimensionLabels({ with_usage: true });
      setRows(r.items || []);
    } catch (e: any) {
      toast.error(e.message || '加载维度标签失败');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  // 已有的主题 / 维度(名称 + 编码成对),新增时从这里选
  const topics = useMemo(() => {
    const m = new Map<string, { topic: string; topic_id: string; count: number }>();
    rows.forEach((r) => {
      const t = m.get(r.topic_id) || { topic: r.topic, topic_id: r.topic_id, count: 0 };
      t.count += 1;
      m.set(r.topic_id, t);
    });
    return [...m.values()];
  }, [rows]);

  const dimensionsOf = (topicId: string) => {
    const m = new Map<string, { dimension: string; dimension_id: string }>();
    rows.filter((r) => r.topic_id === topicId)
      .forEach((r) => m.set(r.dimension_id, { dimension: r.dimension, dimension_id: r.dimension_id }));
    return [...m.values()];
  };

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return rows.filter((r) => {
      if (topicFilter && r.topic_id !== topicFilter) return false;
      if (!showInactive && !r.active) return false;
      if (!kw) return true;
      return [r.topic, r.topic_id, r.dimension, r.dimension_id, r.metric, r.metric_id]
        .some((v) => (v || '').toLowerCase().includes(kw));
    });
  }, [rows, keyword, topicFilter, showInactive]);

  // 按 主题 → 维度 分组展示(与前台问题树同一个形状,运营按这个形状理解它)
  const grouped = useMemo(() => {
    const out: { topic: string; topic_id: string; dims: { dimension: string; dimension_id: string; items: api.DimensionLabel[] }[] }[] = [];
    filtered.forEach((r) => {
      let t = out.find((x) => x.topic_id === r.topic_id);
      if (!t) { t = { topic: r.topic, topic_id: r.topic_id, dims: [] }; out.push(t); }
      let d = t.dims.find((x) => x.dimension_id === r.dimension_id);
      if (!d) { d = { dimension: r.dimension, dimension_id: r.dimension_id, items: [] }; t.dims.push(d); }
      d.items.push(r);
    });
    return out;
  }, [filtered]);

  // ── 新增 / 编辑 ──
  const openCreate = (preset?: Partial<Draft>) => {
    const d = { ...emptyDraft(), ...preset };
    setEditing(d);
    setOriginal(null);
    setTopicPick(d.topic_id || (topics[0]?.topic_id ?? NEW));
    setDimPick(d.dimension_id || NEW);
    if (!preset?.topic_id && topics[0]) {
      setEditing({ ...d, topic: topics[0].topic, topic_id: topics[0].topic_id });
    }
  };
  const openEdit = (r: api.DimensionLabel) => {
    setEditing({ ...r });
    setOriginal(r);
    setTopicPick(NEW);   // 编辑时直接改名称/编码,不走下拉
    setDimPick(NEW);
  };

  const pickTopic = (v: string) => {
    setTopicPick(v);
    setDimPick(NEW);
    if (!editing) return;
    if (v === NEW) setEditing({ ...editing, topic: '', topic_id: '', dimension: '', dimension_id: '' });
    else {
      const t = topics.find((x) => x.topic_id === v)!;
      setEditing({ ...editing, topic: t.topic, topic_id: t.topic_id, dimension: '', dimension_id: '' });
    }
  };
  const pickDim = (v: string) => {
    setDimPick(v);
    if (!editing) return;
    if (v === NEW) setEditing({ ...editing, dimension: '', dimension_id: '' });
    else {
      const d = dimensionsOf(editing.topic_id).find((x) => x.dimension_id === v)!;
      setEditing({ ...editing, dimension: d.dimension, dimension_id: d.dimension_id });
    }
  };

  const problems = (d: Draft): string[] => {
    const p: string[] = [];
    const need: [keyof Draft, string][] = [['topic', '主题名称'], ['topic_id', '主题编码'],
      ['dimension', '维度名称'], ['dimension_id', '维度编码'], ['metric', '指标名称'], ['metric_id', '指标编码']];
    need.forEach(([k, label]) => { if (!String(d[k] ?? '').trim()) p.push(`${label}不能为空`); });
    (['topic_id', 'dimension_id', 'metric_id'] as const).forEach((k) => {
      const v = String(d[k] || '').trim();
      if (v && !CODE_RE.test(v)) p.push(`编码「${v}」只能用字母、数字、下划线,且以字母开头`);
    });
    // 新建主题/维度时,编码撞上了已有的另一个名称 —— 这正是「凭空多出一个同名主题」的反面:
    // 同一个编码两个名字,前台按编码分组时两条路径会被混成一条
    if (topicPick === NEW && !original) {
      const clash = topics.find((t) => t.topic_id === d.topic_id.trim() && t.topic !== d.topic.trim());
      if (clash) p.push(`主题编码「${clash.topic_id}」已被「${clash.topic}」使用,请直接在上面选它,或换一个编码`);
      const sameName = topics.find((t) => t.topic === d.topic.trim() && t.topic_id !== d.topic_id.trim());
      if (sameName) p.push(`已有主题「${sameName.topic}」(编码 ${sameName.topic_id}),新建会出现两个同名主题,请直接在上面选它`);
    }
    return p;
  };

  const save = async () => {
    if (!editing) return;
    const errs = problems(editing);
    if (errs.length) { toast.error(errs[0]); return; }
    const body: api.DimensionLabelDraft = {
      topic: editing.topic.trim(), topic_id: editing.topic_id.trim(),
      dimension: editing.dimension.trim(), dimension_id: editing.dimension_id.trim(),
      metric: editing.metric.trim(), metric_id: editing.metric_id.trim(),
      sort_order: Number(editing.sort_order) || 0, active: !!editing.active,
    };
    setSaving(true);
    try {
      if (original) {
        await api.updateDimensionLabel(original.id, body);
        toast.success(renameCount > 0 ? `已保存,并同步修改了 ${renameCount} 个已挂载的问题` : '已保存');
      } else {
        await api.createDimensionLabel(body);
        toast.success('已新增');
      }
      setEditing(null);
      await load();
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  // 编辑时名称/编码变了,且下面挂着问题:这些问题会被后端一并改过去,先说清楚
  const renamed = !!(original && editing && (
    original.topic !== editing.topic.trim() || original.topic_id !== editing.topic_id.trim() ||
    original.dimension !== editing.dimension.trim() || original.dimension_id !== editing.dimension_id.trim() ||
    original.metric !== editing.metric.trim() || original.metric_id !== editing.metric_id.trim()));
  const renameCount = renamed ? (original?.issue_count || 0) : 0;

  const toggleActive = async (r: api.DimensionLabel) => {
    setBusyId(r.id);
    try {
      await api.updateDimensionLabel(r.id, { active: !r.active });
      toast.success(r.active ? '已停用:它不再出现在前台选择器里,已挂的问题保留' : '已启用');
      await load();
    } catch (e: any) {
      toast.error(e.message || '操作失败');
    } finally {
      setBusyId(null);
    }
  };

  const doDelete = async () => {
    if (!deleting) return;
    setBusyId(deleting.id);
    try {
      const r = await api.deleteDimensionLabel(deleting.id);
      toast.success(r.message || '已删除');
      setDeleting(null);
      await load();
    } catch (e: any) {
      toast.error(e.message || '删除失败');
    } finally {
      setBusyId(null);
    }
  };

  const total = rows.length;
  const activeCount = rows.filter((r) => r.active).length;
  const usedCount = rows.filter((r) => (r.issue_count || 0) > 0).length;

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-slate-900">维度标签</h1>
          <p className="mt-1 text-sm text-slate-500">
            问题体系的「主题 → 维度 → 指标」三级路径。APP 问题树、快捷问题与 Quick BI 固定问题都挂在这里。
            全平台共用,改动会影响所有景区。
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" onClick={load} disabled={loading}>
            <RefreshCw size={14} className={cn('mr-1', loading && 'animate-spin')} />刷新
          </Button>
          <Button onClick={() => openCreate()} disabled={!canWrite}
                  title={canWrite ? undefined : whyDisabled(cap, null, '修改')}>
            <Plus size={14} className="mr-1" />新增标签
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3">
        {[['标签总数', total], ['启用中', activeCount], ['已挂问题', usedCount]].map(([k, v]) => (
          <Card key={k as string} className="p-4">
            <div className="text-xs text-slate-500">{k}</div>
            <div className="mt-1 text-2xl font-bold text-slate-900">{v}</div>
          </Card>
        ))}
      </div>

      <Card className="p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="relative min-w-0 flex-1">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <Input value={keyword} onChange={(e) => setKeyword(e.target.value)}
                   placeholder="搜索名称或编码" className="pl-9" />
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <button onClick={() => setTopicFilter('')}
              className={cn('rounded-full px-3 py-1 text-xs whitespace-nowrap',
                !topicFilter ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200')}>
              全部主题
            </button>
            {topics.map((t) => (
              <button key={t.topic_id} onClick={() => setTopicFilter(t.topic_id)}
                className={cn('rounded-full px-3 py-1 text-xs whitespace-nowrap',
                  topicFilter === t.topic_id ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200')}>
                {t.topic} · {t.count}
              </button>
            ))}
          </div>
          <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs text-slate-600">
            <input type="checkbox" checked={showInactive} onChange={(e) => setShowInactive(e.target.checked)} />
            显示已停用
          </label>
        </div>
      </Card>

      {loading ? (
        <div className="flex items-center justify-center py-20 text-slate-400">
          <Loader2 size={18} className="mr-2 animate-spin" />加载中…
        </div>
      ) : grouped.length === 0 ? (
        <Card className="py-16 text-center text-sm text-slate-400">
          {rows.length === 0 ? '还没有任何维度标签' : '没有符合筛选条件的标签'}
        </Card>
      ) : (
        <div className="space-y-4">
          {grouped.map((t) => (
            <Card key={t.topic_id} className="overflow-hidden">
              <div className="flex items-center justify-between gap-2 border-b border-slate-100 bg-slate-50/60 px-4 py-3">
                <div className="flex min-w-0 items-center gap-2">
                  <Layers size={15} className="shrink-0 text-blue-600" />
                  <span className="font-semibold text-slate-800">{t.topic}</span>
                  <code className="truncate text-xs text-slate-400">{t.topic_id}</code>
                </div>
                <Button size="sm" variant="ghost" disabled={!canWrite}
                        onClick={() => openCreate({ topic: t.topic, topic_id: t.topic_id })}
                        title={canWrite ? `在「${t.topic}」下新增` : whyDisabled(cap, null, '修改')}>
                  <Plus size={13} className="mr-1" />在此主题下新增
                </Button>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[720px] text-sm">
                  <thead>
                    <tr className="text-left text-xs text-slate-500">
                      <th className="px-4 py-2 font-medium">维度</th>
                      <th className="px-4 py-2 font-medium">指标</th>
                      <th className="px-4 py-2 font-medium">排序</th>
                      <th className="px-4 py-2 font-medium">挂载问题</th>
                      <th className="px-4 py-2 font-medium">状态</th>
                      <th className="px-4 py-2 text-right font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {t.dims.map((d) => d.items.map((r, i) => {
                      const used = r.issue_count || 0;
                      return (
                        <tr key={r.id} className={cn('border-t border-slate-50', !r.active && 'opacity-60')}>
                          <td className="px-4 py-2.5 align-top">
                            {i === 0 && (
                              <div>
                                <div className="text-slate-800">{d.dimension}</div>
                                <code className="text-xs text-slate-400">{d.dimension_id}</code>
                              </div>
                            )}
                          </td>
                          <td className="px-4 py-2.5">
                            <div className="text-slate-800">{r.metric}</div>
                            <code className="text-xs text-slate-400">{r.metric_id}</code>
                          </td>
                          <td className="px-4 py-2.5 text-slate-600">{r.sort_order}</td>
                          <td className="px-4 py-2.5">
                            {used > 0
                              ? <Badge variant="outline" className="border-blue-200 bg-blue-50 text-blue-700">{used} 个</Badge>
                              : <span className="text-xs text-slate-400">无</span>}
                          </td>
                          <td className="px-4 py-2.5">
                            <button disabled={!canWrite || busyId === r.id} onClick={() => toggleActive(r)}
                              title={canWrite ? (r.active ? '点击停用' : '点击启用') : whyDisabled(cap, null, '修改')}
                              className={cn('rounded-full px-2.5 py-0.5 text-xs whitespace-nowrap',
                                r.active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500',
                                canWrite && 'hover:ring-1 hover:ring-slate-300')}>
                              {busyId === r.id ? '…' : r.active ? '启用' : '已停用'}
                            </button>
                          </td>
                          <td className="px-4 py-2.5">
                            <div className="flex justify-end gap-1">
                              <Button size="sm" variant="ghost" disabled={!canWrite} onClick={() => openEdit(r)}
                                      title={canWrite ? '编辑' : whyDisabled(cap, null, '修改')}>
                                <Pencil size={13} />
                              </Button>
                              {/* 下面挂着问题就删不掉(删了那些问题会在前台凭空消失)——
                                  直接把按钮置灰并说清原因,不让人点了才知道 */}
                              <Button size="sm" variant="ghost"
                                      disabled={!canDel || used > 0 || busyId === r.id}
                                      onClick={() => setDeleting(r)}
                                      title={!canDel ? whyDisabled(cap, null, '删除')
                                        : used > 0 ? `下面还挂着 ${used} 个问题,请先移走或删除这些问题;只是不想让它出现在前台的话,停用即可`
                                        : '删除'}>
                                <Trash2 size={13} className={cn(canDel && used === 0 && 'text-red-500')} />
                              </Button>
                            </div>
                          </td>
                        </tr>
                      );
                    }))}
                  </tbody>
                </table>
              </div>
            </Card>
          ))}
        </div>
      )}

      {editing && (
        <Modal title={original ? '编辑维度标签' : '新增维度标签'} wide onClose={() => setEditing(null)}
          footer={<>
            <Button variant="outline" onClick={() => setEditing(null)} disabled={saving}>取消</Button>
            <Button onClick={save} disabled={saving}>
              {saving && <Loader2 size={14} className="mr-1 animate-spin" />}保存
            </Button>
          </>}>
          {/* 主题 */}
          <div className="space-y-2">
            <div className="text-sm font-medium text-slate-700">主题</div>
            {!original && (
              <select value={topicPick} onChange={(e) => pickTopic(e.target.value)}
                      className="h-9 w-full rounded-md border border-slate-200 px-2 text-sm">
                {topics.map((t) => <option key={t.topic_id} value={t.topic_id}>{t.topic}({t.topic_id})</option>)}
                <option value={NEW}>+ 新建一个主题</option>
              </select>
            )}
            {(original || topicPick === NEW) && (
              <div className="grid grid-cols-2 gap-2">
                <Input placeholder="主题名称,如 客流" value={editing.topic}
                       onChange={(e) => setEditing({ ...editing, topic: e.target.value })} />
                <Input placeholder="主题编码,如 visitorFlow" value={editing.topic_id}
                       onChange={(e) => setEditing({ ...editing, topic_id: e.target.value })} />
              </div>
            )}
          </div>

          {/* 维度 */}
          <div className="space-y-2">
            <div className="text-sm font-medium text-slate-700">维度</div>
            {!original && topicPick !== NEW && dimensionsOf(editing.topic_id).length > 0 && (
              <select value={dimPick} onChange={(e) => pickDim(e.target.value)}
                      className="h-9 w-full rounded-md border border-slate-200 px-2 text-sm">
                <option value={NEW}>+ 新建一个维度</option>
                {dimensionsOf(editing.topic_id).map((d) =>
                  <option key={d.dimension_id} value={d.dimension_id}>{d.dimension}({d.dimension_id})</option>)}
              </select>
            )}
            {(original || dimPick === NEW || topicPick === NEW || dimensionsOf(editing.topic_id).length === 0) && (
              <div className="grid grid-cols-2 gap-2">
                <Input placeholder="维度名称,如 在园人员" value={editing.dimension}
                       onChange={(e) => setEditing({ ...editing, dimension: e.target.value })} />
                <Input placeholder="维度编码,如 inParkVisitor" value={editing.dimension_id}
                       onChange={(e) => setEditing({ ...editing, dimension_id: e.target.value })} />
              </div>
            )}
          </div>

          {/* 指标 */}
          <div className="space-y-2">
            <div className="text-sm font-medium text-slate-700">指标</div>
            <div className="grid grid-cols-2 gap-2">
              <Input placeholder="指标名称,如 实时人数" value={editing.metric}
                     onChange={(e) => setEditing({ ...editing, metric: e.target.value })} />
              <Input placeholder="指标编码,如 liveVisitorCnt" value={editing.metric_id}
                     onChange={(e) => setEditing({ ...editing, metric_id: e.target.value })} />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <div className="text-sm font-medium text-slate-700">排序</div>
              <Input type="number" value={String(editing.sort_order ?? 0)}
                     onChange={(e) => setEditing({ ...editing, sort_order: Number(e.target.value) || 0 })} />
              <div className="text-xs text-slate-400">同一维度下小的排前面</div>
            </div>
            <label className="flex items-center gap-2 self-center pt-5 text-sm text-slate-700">
              <input type="checkbox" checked={!!editing.active}
                     onChange={(e) => setEditing({ ...editing, active: e.target.checked })} />
              启用(停用后不出现在前台选择器,已挂的问题保留)
            </label>
          </div>

          <p className="text-xs text-slate-500">
            名称给人看,编码给程序分组。编码只能用字母、数字、下划线,且以字母开头;
            名称三元组与编码三元组都不能和已有标签重复。
          </p>

          {renameCount > 0 && (
            <div className="flex gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <span>这个标签下挂着 <b>{renameCount}</b> 个问题。改了名称或编码,这些问题会被一并改到新路径上
                (所有景区都会看到变化)。只想让它不出现在前台的话,停用即可。</span>
            </div>
          )}
          {problems(editing).length > 0 && (editing.metric || editing.metric_id) && (
            <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
              {problems(editing).map((p) => <div key={p}>· {p}</div>)}
            </div>
          )}
        </Modal>
      )}

      {deleting && (
        <ConfirmModal title="删除维度标签" danger confirmLabel="删除" loading={busyId === deleting.id}
          message={`确定删除「${deleting.topic} / ${deleting.dimension} / ${deleting.metric}」?删除不可恢复。`}
          onConfirm={doDelete} onCancel={() => setDeleting(null)} />
      )}
    </div>
  );
};

export default DimensionLabels;
