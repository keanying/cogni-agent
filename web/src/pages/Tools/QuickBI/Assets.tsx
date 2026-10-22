import React, { useEffect, useRef, useState } from 'react';
import {
  Database, Download, Eye, FileSpreadsheet, LayoutDashboard, ListPlus, Loader2, Pencil, Plus, RefreshCw, Search, Trash2, Upload,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import { Badge } from '../../../components/ui/badge';
import { Modal, ConfirmModal, CopyId } from '../../../components/ui/PageHelpers';
import { confirmDialog } from '../../../components/ui/confirm';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import { ConnectionSelect, ScenicSelect, inputCls, labelCls } from './shared';
import { canEditRow, useCapabilities } from '../../../lib/capabilities';
import { chartLabel, dataCharts } from './chartScope';
import { categoriesOf, groupByCategory } from './datasetCategory';

const DEMO_URL = 'https://bi.aliyuncs.com/token3rd/dashboard/view/pc.htm?pageId=88c9e5de-2d4d-4ba9-a045-6f9b71cf0c66&accessTicket=807cc156-9109-4dfc-b4d7-ce5c670ba566&dd_orientation=auto';

const DashboardDetail: React.FC<{ summary: any }> = ({ summary }) => {
  // 只列带数据的图表:容器 / 页签 / 文本组件没有数据,名字还常是一串 uuid,列出来只会干扰
  // (「图表 93」里混着这些,对不上 Quick BI 解读里能选的数量)
  const charts = dataCharts(summary.charts);
  const others = (summary.charts?.length || 0) - charts.length;
  return (
  <div className="space-y-3 text-xs">
    <div className="flex flex-wrap gap-3 text-slate-500">
      <span>pageId <code className="font-mono text-slate-700">{summary.page_id}</code></span>
      <span>图表 {charts.length}{others > 0 ? `(另有 ${others} 个容器/布局组件)` : ''}</span>
      <span>数据集 {summary.cubes?.length || 0}</span>
      <span>查询控件 {summary.query_controls?.length || 0}</span>
      {summary.view_url && <a className="text-blue-600 underline" href={summary.view_url} target="_blank" rel="noreferrer">在 Quick BI 打开</a>}
    </div>
    <div>
      <div className={labelCls}>引用的数据集</div>
      <div className="flex flex-wrap gap-1.5">
        {(summary.cubes || []).map((c: any) => (
          <span key={c.cube_id} className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1">{c.cube_name}
            <span className="ml-1 font-mono text-[10px] text-slate-400">{c.cube_id}</span></span>
        ))}
      </div>
    </div>
    <div>
      <div className={labelCls}>图表</div>
      <div className="max-h-56 overflow-auto rounded-lg border border-slate-100">
        <table className="w-full">
          <thead className="sticky top-0 bg-slate-50 text-left text-slate-500"><tr>
            <th className="px-2 py-1">名称</th><th className="px-2 py-1">类型</th><th className="px-2 py-1">维度</th><th className="px-2 py-1">度量</th>
          </tr></thead>
          <tbody>
            {charts.map((c: any) => (
              <tr key={c.component_id} className="border-t border-slate-100">
                <td className="px-2 py-1 font-medium text-slate-700">{chartLabel(c)}</td>
                <td className="px-2 py-1 text-slate-400">{c.chart_type}</td>
                <td className="px-2 py-1">{c.dimensions.map((d: any) => d.name).join('、')}</td>
                <td className="px-2 py-1">{c.measures.map((m: any) => m.name + (m.aggregate ? `(${m.aggregate})` : '')).join('、')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
    {(summary.query_controls || []).length > 0 && (
      <div>
        <div className={labelCls}>查询控件</div>
        {(summary.query_controls || []).flatMap((q: any) => q.fields).map((f: any, i: number) => (
          <span key={i} className="mr-1.5 inline-block rounded border border-slate-200 px-1.5 py-0.5">
            {f.label}{f.required ? '*' : ''}<span className="text-slate-400"> {f.widget}</span></span>
        ))}
      </div>
    )}
    {(summary.suggested_questions || []).length > 0 && (
      <div>
        <div className={labelCls}>候选固定问题</div>
        <ul className="list-disc space-y-0.5 pl-5 text-slate-600">
          {summary.suggested_questions.map((q: any, i: number) => <li key={i}>{q.question}<span className="ml-1 text-slate-400">[{q.capability}]</span></li>)}
        </ul>
      </div>
    )}
  </div>
  );
};

const Assets: React.FC<{ connections: api.QbiConnection[]; onAdopted: () => void }> = ({ connections, onAdopted }) => {
  const [connId, setConnId] = useState('');
  const [assets, setAssets] = useState<api.QbiAsset[]>([]);
  const cap = useCapabilities();
  // 编辑已登记资产:名称 + 归属景区
  const [editing, setEditing] = useState<{ id: string; name: string; scenic_id: string } | null>(null);
  const [savingAsset, setSavingAsset] = useState(false);

  const saveAsset = async () => {
    if (!editing) return;
    setSavingAsset(true);
    try {
      await api.qbiUpdateAsset(editing.id, { name: editing.name, scenic_id: editing.scenic_id });
      toast.success('已保存;指着这个资产的问题不受影响,照常可用');
      setEditing(null);
      load();
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSavingAsset(false); }
  };
  const [files, setFiles] = useState<api.QbiFile[]>([]);
  const [cubes, setCubes] = useState<{ cube_id: string; cube_name: string; registered?: boolean }[] | null>(null);
  const [cubeKw, setCubeKw] = useState('');
  // 从哪块仪表板拉(空 = 整条连接下的全部 cube,兜底用)
  const [pullBoard, setPullBoard] = useState('');
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(false);
  const [dashUrl, setDashUrl] = useState('');
  const [dashName, setDashName] = useState('');
  // 资产绑定的景区:提问时按景区找看板,所以登记时就得说清这块看板归谁。
  // 留空 = 集团级,所有景区都能用。
  const [scenicId, setScenicId] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [preview, setPreview] = useState<any>(null);
  const [detail, setDetail] = useState<api.QbiAsset | null>(null);
  // 删除清单:数据集与问题都挂在仪表板下面,删板 = 连带删它们。
  // 不先摆出来的话,人按下「确定」时并不知道自己同意了删掉 17 个集、12 条问题。
  const [delPlan, setDelPlan] = useState<api.QbiAssetDeletePlan | null>(null);
  const [deleting, setDeleting] = useState(false);

  const askDelete = async (id: string) => {
    try { setDelPlan(await api.qbiAssetDeletePlan(id)); }
    // 清单拿不到时**不要**退化成「那就直接删吧」—— 那正是这轮要根治的东西
    catch (e: any) { toast.error(e.message || '读取不到这个资产下面挂着什么,先不删'); }
  };

  const doDelete = async () => {
    if (!delPlan) return;
    setDeleting(true);
    try {
      const r = await api.qbiDeleteAsset(delPlan.asset.id, true);
      toast.success(r.message || '已删除');
      setDelPlan(null);
      load();
    } catch (e: any) { toast.error(e.message || '删除失败'); }
    finally { setDeleting(false); }
  };
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!connId && connections.length) setConnId((connections.find((c) => c.is_default) || connections[0]).id);
  }, [connections, connId]);

  // 列表里出现过哪些景区就按 ID 取哪些名字 —— 不再依赖「已登记清单」那一个接口:
  // 资产可能绑在一个后来被停用的景区上,清单里没有它,卡片上就只剩一串编号
  const [scenicNames, setScenicNames] = useState<Record<string, string>>({});
  const scenicName = (id: string) => scenicNames[id] || id;
  useEffect(() => {
    const ids = Array.from(new Set(assets.map((a) => a.scenic_id).filter(Boolean))) as string[];
    const missing = ids.filter((id) => !scenicNames[id]);
    if (!missing.length) return;
    Promise.all(missing.map((id) => api.getScenicCatalogDetail(id)
      .then((r) => [id, r.scenic_name || id] as const).catch(() => [id, id] as const)))
      .then((pairs) => setScenicNames((prev) => ({ ...prev, ...Object.fromEntries(pairs) })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assets]);

  // 景区角色新登记的资产只能挂在自己景区下,预填上
  useEffect(() => {
    if (!cap.is_group && cap.scope_scenic_id) setScenicId((v) => v || cap.scope_scenic_id!);
  }, [cap.is_group, cap.scope_scenic_id]);

  const load = async () => {
    if (!connId) return;
    setLoading(true);
    try {
      const [a, f] = await Promise.all([api.qbiListAssets({ connection_id: connId }), api.qbiListFiles(connId)]);
      setAssets(a.items); setFiles(f.items);
    } catch (e: any) { toast.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { setCubes(null); load(); /* eslint-disable-next-line */ }, [connId]);

  const run = async (key: string, fn: () => Promise<any>, ok?: string) => {
    setBusy(key);
    try { const r = await fn(); if (ok) toast.success(ok); return r; }
    catch (e: any) { toast.error(e.message || '操作失败'); return null; }
    finally { setBusy(null); }
  };

  const dashboards = assets.filter((a) => a.kind === 'dashboard');
  const datasets = assets.filter((a) => a.kind === 'dataset');
  // 数据集归到它所属的仪表板下(板的 meta.cubes 写着用到哪些 cube);都没认领的单独一组
  // 已登记判定:按仪表板拉时后端直接给了 registered,拉全部时按本地已登记列表判
  const isRegistered = (cubeId: string) =>
    (cubes || []).find((c) => c.cube_id === cubeId)?.registered ?? datasets.some((d) => d.ref_id === cubeId);
  const visibleCubes = (cubes || []).filter((c) => !cubeKw || c.cube_name.includes(cubeKw));
  const pickedCount = visibleCubes.filter((c) => picked[c.cube_id] && !isRegistered(c.cube_id)).length;

  const datasetGroups = React.useMemo(() => {
    const taken = new Set<string>();
    const groups = dashboards.map((b) => {
      // 登记时记了来源板的按它算(准);历史数据没有,才退回按 cube 反查 ——
      // 反查在「两块板共用一个 cube」「板刷新过结构」这两种情况下会算错
      const cubeIds = new Set((b.cubes || []).map((c) => c.cube_id));
      const mine = datasets.filter((d) => d.source_asset_id
        ? d.source_asset_id === b.id
        : cubeIds.has(d.ref_id));
      mine.forEach((d) => taken.add(d.id));
      return { key: b.id, label: `${b.name}${b.scenic_id ? '' : '（集团级）'}`, items: mine };
    }).filter((g) => g.items.length > 0);
    const orphans = datasets.filter((d) => !taken.has(d.id));
    if (orphans.length) groups.push({ key: '__orphan__', label: '未归属仪表板', items: orphans });
    return groups;
  }, [assets]);

  // ── 自定义分类:勾选数据集 → 输入分类回车即存(不需要先建分类);展示可按分类 / 按仪表板分组 ──
  const categories = React.useMemo(() => categoriesOf(datasets), [assets]);
  const [groupMode, setGroupMode] = useState<'category' | 'board' | null>(null);
  const mode = groupMode ?? (categories.length ? 'category' : 'board');   // 有分类就默认按分类
  const shownGroups = mode === 'category'
    ? groupByCategory(datasets).map((g) => ({ ...g, key: `cat:${g.key}` }))
    : datasetGroups;
  const [catPicked, setCatPicked] = useState<Set<string>>(new Set());
  const [catInput, setCatInput] = useState('');
  const toggleCat = (id: string) => setCatPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const applyCategory = async (category: string) => {
    const ids = Array.from(catPicked);
    if (!ids.length) return;
    const r = await run('category', () => api.qbiSetAssetsCategory(ids, category),
      category.trim() ? `已把 ${ids.length} 个数据集归到「${category.trim()}」` : `已清掉 ${ids.length} 个数据集的分类`);
    if (r) { setCatPicked(new Set()); setCatInput(''); load(); }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-72"><label className={labelCls}>连接</label>
          <ConnectionSelect items={connections} value={connId} onChange={setConnId} /></div>
        <Button variant="outline" onClick={load} className="gap-1.5"><RefreshCw size={14} />刷新</Button>
      </div>

      {/* ── 仪表板(报表能力的目标数据) ── */}
      <section className="rounded-2xl border border-slate-200 bg-white p-4">
        <div className="mb-3 flex items-center gap-2 font-bold text-slate-800"><LayoutDashboard size={16} className="text-orange-500" />已有仪表板（报表）</div>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-[1fr_180px_170px_auto_auto]">
          <input className={inputCls} placeholder="仪表板地址 / 数据门户地址 / pageId(支持 token3rd 嵌入链接)" value={dashUrl}
            onChange={(e) => setDashUrl(e.target.value)} />
          <input className={inputCls} placeholder="显示名称(可选)" value={dashName} onChange={(e) => setDashName(e.target.value)} />
          {/* 归属景区:换成可输入搜索的下拉 —— 原来是只能翻的 <select>,
              景区一多就得一个个找;而且它只列了「已登记」清单,搜不到主数据里的别名/编号。
              留空 = 集团级,所有景区共用这块看板。 */}
          <ScenicSelect value={scenicId} onChange={setScenicId} registeredOnly
            placeholder="集团级(所有景区)"
            disabled={!cap.manage_all && !cap.is_group} />
          <Button variant="outline" disabled={!dashUrl || busy === 'parse'} className="gap-1.5"
            onClick={async () => { const r = await run('parse', () => api.qbiParseDashboard(dashUrl, connId)); if (r) setPreview(r); }}>
            {busy === 'parse' ? <Loader2 size={14} className="animate-spin" /> : <Eye size={14} />}预览结构
          </Button>
          <Button disabled={!dashUrl || busy === 'reg'} className="gap-1.5 bg-[#2563eb]"
            onClick={async () => {
              const r = await run('reg', () => api.qbiRegisterDashboard(dashUrl, connId, dashName || undefined,
                                                                         scenicId || null), '仪表板已登记');
              if (r) { setDashUrl(''); setDashName(''); load(); setDetail(r); }
            }}>
            {busy === 'reg' ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}登记
          </Button>
        </div>
        <button type="button" className="mt-1 text-[11px] text-blue-500 underline" onClick={() => setDashUrl(DEMO_URL)}>填入示例仪表板</button>
        <div className="mt-3 divide-y divide-slate-100">
          {dashboards.length === 0 && !loading && <div className="py-4 text-center text-xs text-slate-400">暂无登记的仪表板</div>}
          {dashboards.map((a) => (
            <div key={a.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5">
              <div className="min-w-0">
                <div className="font-medium text-slate-800">{a.name}</div>
                <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                  <span>图表 {a.chart_count} · 数据集 {a.cube_count}</span>
                  <span className={a.scenic_id ? 'rounded bg-orange-50 px-1.5 text-orange-600' : 'rounded bg-slate-100 px-1.5 text-slate-500'}
                    title={a.scenic_id ? `归属景区 ${a.scenic_id}` : '集团级:所有景区都能用这块看板'}>
                    {a.scenic_id ? scenicName(a.scenic_id) : '集团级'}
                  </span>
                  <span className="truncate">{a.cubes.map((c) => c.cube_name).join('、')}</span>
                  <CopyId id={a.id} />
                </div>
              </div>
              <div className="flex gap-1.5">
                {/* 改归属:没有这一步的话,改归属只能「删了重登记」—— 而删掉的那一刻,
                    所有指着这个 asset_id 的问题全部失效,发起时报「资产不存在」,
                    运营根本不会把它和刚才那次删除联系起来。这里改的是同一行,问题一条都不用动。 */}
                <Button size="sm" variant="outline" className="gap-1" title="修改名称 / 归属景区"
                  disabled={!canEditRow(cap, a.scenic_id)}
                  onClick={() => setEditing({ id: a.id, name: a.name, scenic_id: a.scenic_id || '' })}>
                  <Pencil size={12} />编辑
                </Button>
                <Button size="sm" variant="outline" onClick={async () => setDetail(await api.qbiGetAsset(a.id))}><Eye size={12} /></Button>
                <Button size="sm" variant="outline" disabled={busy === `ref:${a.id}`} title="重新拉取结构"
                  onClick={async () => { await run(`ref:${a.id}`, () => api.qbiRefreshAsset(a.id), '已刷新'); load(); }}>
                  {busy === `ref:${a.id}` ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                </Button>
                <Button size="sm" variant="outline" className="gap-1" disabled={busy === `adopt:${a.id}`}
                  onClick={async () => {
                    const r = await run(`adopt:${a.id}`, () => api.qbiAdoptQuestions(a.id));
                    if (r) { toast.success(`已生成 ${r.items.length} 个固定问题`); onAdopted(); }
                  }}>
                  <ListPlus size={12} />生成固定问题
                </Button>
                <Button size="sm" variant="ghost" className="text-rose-500"
                  onClick={() => askDelete(a.id)}><Trash2 size={12} /></Button>
              </div>
            </div>
          ))}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
        {/* ── 数据集 ── */}
        <section className="rounded-2xl border border-slate-200 bg-white p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 font-bold text-slate-800"><Database size={16} className="text-blue-500" />数据集</div>
            <div className="flex flex-wrap items-center gap-1.5">
              {/* 按仪表板拉,而不是拉整条连接下的全部 cube。
                  一条连接上挂着两块板、几十个 cube,人得自己认哪个属于哪块板 ——
                  认错了就是拿 A 板的集去问 B 板的问题,答得出来,还不报错。
                  选「全部」是兜底:有些集不属于任何已登记的板。 */}
              <select value={pullBoard} onChange={(e) => { setPullBoard(e.target.value); setCubes(null); setPicked({}); }}
                className="h-7 max-w-[150px] rounded-md border border-slate-200 bg-white px-1.5 text-[11px] text-slate-700">
                <option value="">全部可问数数据集</option>
                {dashboards.map((b) => <option key={b.id} value={b.id}>按仪表板：{b.name}</option>)}
              </select>
              <Button size="sm" variant="outline" disabled={busy === 'cubes'} className="gap-1"
                onClick={async () => {
                  setPicked({});
                  if (pullBoard) {
                    const r = await run('cubes', () => api.qbiBoardCubes(pullBoard));
                    if (r) setCubes(r.items);
                  } else {
                    const r = await run('cubes', () => api.qbiListCubes(connId));
                    if (r) setCubes(r.items);
                  }
                }}>
                {busy === 'cubes' ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />}拉取
              </Button>
              {/* 一键登记全部:走后端一次登记,不是前端逐个登记 —— 几十上百个逐个发请求,中途失败会
                  留下一半且说不清哪些成功;已登记的后端按 cube_id 跳过,不会堆重复行。 */}
              <Button size="sm" variant="outline" disabled={busy === 'dsall' || !connId} className="gap-1"
                title={`登记该连接下全部可问数数据集;归属取上方的景区选择(当前:${scenicId || '集团级'})`}
                onClick={async () => {
                  if (!await confirmDialog({ title: '一键登记全部数据集', confirmLabel: '登记全部',
                    message: '会把当前连接下所有可问数的数据集登记进来,已登记的自动跳过。归属取页面上方的景区选择。' })) return;
                  const r = await run('dsall', () => api.qbiRegisterDatasets({ connection_id: connId, cubes: [], all: true,
                    scenic_id: scenicId || null }));
                  if (r) { toast.success(r.message || `已登记 ${r.created} 个`); load(); }
                }}>
                {busy === 'dsall' ? <Loader2 size={12} className="animate-spin" /> : <ListPlus size={12} />}登记全部
              </Button>
              {/* 清空只清数据集:仪表板与上传文件是另一类资产,顺手一起删了属于误伤 */}
              <Button size="sm" variant="outline" disabled={busy === 'dsclear' || datasets.length === 0}
                className="gap-1 text-rose-500" title="清空当前连接下已登记的数据集(还被问题引用的会跳过)"
                onClick={async () => {
                  if (!await confirmDialog({ title: '清空全部数据集', confirmLabel: '清空', danger: true,
                    message: `将删除当前连接下已登记的数据集(仪表板与上传文件不受影响)。`
                      + '还被固定问题当目标数据引用的会跳过 —— 要删它们请逐个删除并确认连带删除的问题。' })) return;
                  const r = await run('dsclear', () => api.qbiClearDatasets(connId));
                  if (r) {
                    toast.success(`已清空 ${r.deleted} 个${r.skipped ? `,${r.skipped} 个仍被问题引用或无权删除,已跳过` : ''}`);
                    load();
                  }
                }}>
                {busy === 'dsclear' ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}清空
              </Button>
            </div>
          </div>
          {/* 已登记的数据集**按仪表板分组**。
              混成一个大列表之后,「这个集属于哪块板」只能靠名字猜 —— 两块板各十几个集时根本分不清,
              很容易拿 A 板的集去问 B 板的问题,而答出来还像模像样,不会报错。
              板的 meta.cubes 里就写着自己用到哪些 cube,按它归位即可;都没认领的单独一组。 */}
          {datasets.length > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
              <span>分组</span>
              {(['category', 'board'] as const).map((m) => (
                <button key={m} type="button" onClick={() => setGroupMode(m)}
                  className={cn('rounded-md border px-2 py-0.5',
                    mode === m ? 'border-blue-200 bg-blue-50 text-blue-600' : 'border-slate-200 hover:bg-slate-50')}>
                  {m === 'category' ? '按分类' : '按仪表板'}
                </button>
              ))}
              <span className="text-slate-300">点数据集可勾选,再输入分类回车</span>
            </div>
          )}
          {/* 勾了数据集:输入分类回车即存(不需要先建分类),空着回车 = 清掉分类 */}
          {catPicked.size > 0 && (
            <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-blue-100 bg-blue-50/60 px-2.5 py-2">
              <span className="text-xs text-slate-600">已选 {catPicked.size} 个</span>
              <input className={cn(inputCls, 'h-8 w-48 text-xs')} list="qbi-dataset-categories" autoFocus
                placeholder="输入分类,回车保存" value={catInput}
                onChange={(e) => setCatInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); applyCategory(catInput); } }} />
              <datalist id="qbi-dataset-categories">{categories.map((c) => <option key={c} value={c} />)}</datalist>
              <button type="button" className="text-[11px] text-slate-500 underline" onClick={() => applyCategory('')}>清掉分类</button>
              <button type="button" className="text-[11px] text-slate-400" onClick={() => setCatPicked(new Set())}>取消选择</button>
              {busy === 'category' && <Loader2 size={12} className="animate-spin text-slate-400" />}
            </div>
          )}
          {/* 已登记的数据集:按分类(运营自定义)或按仪表板分组。
              按仪表板:板的 meta.cubes 里就写着自己用到哪些 cube,按它归位,都没认领的单独一组 ——
              混成一个大列表之后「这个集属于哪块板」只能靠名字猜,很容易拿 A 板的集去问 B 板的问题。 */}
          {shownGroups.map((g) => {
            const ids = g.items.map((d) => d.id);
            const allOn = ids.every((id) => catPicked.has(id));
            return (
            <div key={g.key} className="mb-3">
              <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-bold text-slate-500">
                {mode === 'board' ? <LayoutDashboard size={11} className="text-slate-400" /> : <Database size={11} className="text-slate-400" />}
                {g.label}
                <span className="font-normal text-slate-300">{g.items.length} 个</span>
                <button type="button" className="ml-1 font-normal text-slate-400 hover:text-blue-600"
                  onClick={() => setCatPicked((p) => { const n = new Set(p); ids.forEach((id) => (allOn ? n.delete(id) : n.add(id))); return n; })}>
                  {allOn ? '取消本组' : '选本组'}
                </button>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {g.items.map((d) => (
                  <span key={d.id} onClick={() => toggleCat(d.id)}
                    className={cn('inline-flex cursor-pointer items-center gap-1 rounded-lg border px-2 py-1 text-xs',
                      d.active === false ? 'border-slate-200 bg-slate-50 text-slate-400 line-through'
                        : catPicked.has(d.id) ? 'border-blue-400 bg-blue-100 text-blue-800 ring-1 ring-blue-300'
                          : 'border-blue-100 bg-blue-50 text-blue-700')}
                    title={d.active === false ? (d.disabled_reason || '已停用') : d.name}>
                    {d.name}
                    {mode === 'board' && d.category && (
                      <span className="rounded bg-white/70 px-1 text-[10px] text-slate-500">{d.category}</span>
                    )}
                    <button type="button" className="text-blue-300 hover:text-rose-500"
                      onClick={(e) => { e.stopPropagation(); askDelete(d.id); }}>×</button>
                  </span>
                ))}
              </div>
            </div>
            );
          })}
          {cubes && (
            <>
              <div className="mb-2 flex items-center gap-2">
                <input className={cn(inputCls, 'h-8 flex-1 text-xs')} placeholder="筛选"
                  value={cubeKw} onChange={(e) => setCubeKw(e.target.value)} />
                {/* 多选登记:一块板动辄十几二十个集,一个一个点既慢又容易漏 ——
                    漏掉的那个不会报错,只是到用的时候「怎么选不到这张表」 */}
                <Button size="sm" variant="outline" className="shrink-0 text-[11px]"
                  onClick={() => {
                    const all = visibleCubes.filter((c) => !isRegistered(c.cube_id));
                    const on = all.every((c) => picked[c.cube_id]);
                    setPicked(on ? {} : Object.fromEntries(all.map((c) => [c.cube_id, true])));
                  }}>
                  {pickedCount > 0 ? '取消全选' : '全选未登记'}
                </Button>
                <Button size="sm" className="shrink-0 bg-[#2563eb] text-[11px] hover:bg-[#1d4ed8]"
                  disabled={!pickedCount || busy === 'batch'}
                  onClick={async () => {
                    const list = visibleCubes.filter((c) => picked[c.cube_id])
                      .map((c) => ({ cube_id: c.cube_id, name: c.cube_name }));
                    const r = await run('batch', () => api.qbiRegisterDatasets({
                      connection_id: connId, cubes: list,
                      // 记下来源板:之后分组与「删板连带删集」都按它算,不靠 meta.cubes 反查
                      source_asset_id: pullBoard || null,
                      scenic_id: pullBoard ? undefined : (scenicId || null),
                    }));
                    if (r) { toast.success(r.message); setPicked({}); load(); }
                  }}>
                  {busy === 'batch' ? <Loader2 size={12} className="animate-spin" /> : null}
                  登记选中（{pickedCount}）
                </Button>
              </div>
              <div className="max-h-64 divide-y divide-slate-100 overflow-auto">
                {visibleCubes.map((c) => {
                  const registered = isRegistered(c.cube_id);
                  return (
                    <label key={c.cube_id}
                      className={cn('flex items-center gap-2 py-1.5 text-xs',
                        registered ? 'opacity-50' : 'cursor-pointer hover:bg-slate-50')}>
                      <input type="checkbox" disabled={registered} checked={!!picked[c.cube_id]}
                        onChange={(e) => setPicked((m) => ({ ...m, [c.cube_id]: e.target.checked }))} />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-slate-700">{c.cube_name}</div>
                        <div className="font-mono text-[10px] text-slate-400">{c.cube_id}</div>
                      </div>
                      {registered && <span className="shrink-0 text-[10px] text-slate-400">已登记</span>}
                    </label>
                  );
                })}
                {cubes.length === 0 && <div className="py-4 text-center text-xs text-slate-400">
                  {pullBoard ? '这块仪表板里没有可问数的数据集' : '当前 user_token 没有可问数的数据集'}</div>}
              </div>
              {pullBoard && (
                <p className="mt-2 text-[11px] text-slate-400">
                  登记后会记下「来自这块仪表板」,归属景区默认跟着它走 ——
                  板绑了景区、集却留在集团级,是最容易配漏的一处。
                </p>
              )}
            </>
          )}
          {!cubes && <p className="text-xs text-slate-400">数据集问数不登记也能用(不指定时自动智能选表);登记后可在固定问题 / 操练台里快速选择。</p>}
        </section>

        {/* ── 上传文件 ── */}
        <section className="rounded-2xl border border-slate-200 bg-white p-4">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-2 font-bold text-slate-800"><FileSpreadsheet size={16} className="text-cyan-500" />上传的数据</div>
            <input ref={fileInput} type="file" className="hidden" accept=".xls,.xlsx,.csv,.doc,.docx,.pdf,.png,.jpg,.jpeg,.jp2,.webp,.gif,.bmp,.ppt,.pptx"
              onChange={async (e) => {
                const f = e.target.files?.[0]; e.target.value = '';
                if (!f) return;
                const r = await run('upload', () => api.qbiUploadFile(f, connId), `已上传「${f.name}」`);
                if (r) { (r.warnings || []).forEach((w: string) => toast.warning(w)); load(); }
              }} />
            <Button size="sm" variant="outline" disabled={busy === 'upload' || !connId} className="gap-1" onClick={() => fileInput.current?.click()}>
              {busy === 'upload' ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />}上传(≤5MB)
            </Button>
          </div>
          <div className="max-h-72 divide-y divide-slate-100 overflow-auto">
            {files.length === 0 && <div className="py-4 text-center text-xs text-slate-400">xls / xlsx / csv 用于问数与解读;doc / docx 可作为报告引用;PDF / 图片 / Word / PPT 可做文档解析</div>}
            {files.map((f) => (
              <div key={f.id} className="flex items-center justify-between gap-2 py-2 text-xs">
                <div className="min-w-0">
                  <div className="truncate font-medium text-slate-700">{f.file_name}</div>
                  <div className="flex flex-wrap gap-1 pt-0.5">
                    <span className="text-slate-400">{(f.size / 1024).toFixed(0)}KB</span>
                    {f.qa_ready && <Badge variant="info">问数就绪</Badge>}
                    {f.insight_ready && <Badge variant="secondary">可解读 {f.markdown_chars} 字</Badge>}
                    {f.report_ready && <Badge variant="success">报告已引用</Badge>}
                  </div>
                </div>
                <div className="flex shrink-0 items-center">
                  <Button size="sm" variant="ghost" title="下载"
                    onClick={() => api.qbiDownloadFile(f.id, f.file_name).catch((e) => toast.error(e.message || '下载失败'))}><Download size={12} /></Button>
                  <Button size="sm" variant="ghost" className="text-rose-500"
                    onClick={async () => { await run(`delf:${f.id}`, () => api.qbiDeleteFile(f.id), '已删除'); load(); }}><Trash2 size={12} /></Button>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>

      {preview && (
        <Modal title={`仪表板结构预览:${preview.name || preview.page_id}`} wide onClose={() => setPreview(null)}>
          <DashboardDetail summary={preview} />
        </Modal>
      )}
      {detail && (
        <Modal title={`仪表板:${detail.name}`} wide onClose={() => setDetail(null)}>
          <DashboardDetail summary={detail.meta || {}} />
        </Modal>
      )}
      {editing && (
        <Modal title="编辑数据资产" onClose={() => setEditing(null)}
          footer={<>
            <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
            <Button className="bg-[#2563eb]" disabled={savingAsset} onClick={saveAsset}>
              {savingAsset && <Loader2 size={14} className="mr-1 animate-spin" />}保存
            </Button>
          </>}>
          <div className="space-y-3">
            <div>
              <label className={labelCls}>显示名称</label>
              <input className={inputCls} value={editing.name}
                onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            </div>
            <div>
              <label className={labelCls}>归属景区(留空 = 集团级,所有景区共用)</label>
              <ScenicSelect value={editing.scenic_id} registeredOnly
                placeholder="集团级(所有景区)"
                disabled={!cap.manage_all && !cap.is_group}
                onChange={(v) => setEditing({ ...editing, scenic_id: v })} />
            </div>
            <p className="rounded-lg bg-slate-50 px-3 py-2 text-[11px] leading-relaxed text-slate-500">
              改的是这条资产本身(ID 不变),所以<b className="text-slate-700">指着它的问题一条都不用动</b>,
              照常可用;问题列表上显示的「目标数据」名称会跟着刷新。
              <br />
              归属改成某个景区之后,只有该景区(和集团账号)能用它;留空则所有景区共用。
            </p>
          </div>
        </Modal>
      )}

      {/* 删除确认:把会连带删掉的东西**逐条列出来**再问。
          「确定删除吗」这种问法等于没问 —— 人看不到代价,只能凭印象按确定。 */}
      {delPlan && (
        delPlan.needs_confirm ? (
          <Modal title={`删除「${delPlan.asset.name}」`} onClose={() => !deleting && setDelPlan(null)} wide>
            <div className="space-y-4 text-sm">
              <div className="rounded-xl border border-rose-100 bg-rose-50 p-3 text-xs leading-relaxed text-rose-800">
                数据集与问题都是挂在这块{delPlan.asset.kind === 'dashboard' ? '仪表板' : '数据集'}下面的,
                会<b>一并删除</b>：
                {Object.entries(delPlan.counts).filter(([, v]) => v > 0)
                  .map(([k, v]) => `${k} ${v} 个`).join('、')}。
                删除后 <b>APP 与管理端都不再显示</b>,且不可恢复。
              </div>

              {delPlan.datasets.length > 0 && (
                <div>
                  <div className={labelCls}>将一并删除的数据集（{delPlan.datasets.length}）</div>
                  <div className="mt-1 max-h-32 overflow-y-auto rounded-xl border border-slate-200 divide-y divide-slate-50">
                    {delPlan.datasets.map((d) => (
                      <div key={d.id} className="truncate px-3 py-1.5 text-xs text-slate-600">{d.name}</div>
                    ))}
                  </div>
                  <p className="mt-1 text-[11px] text-slate-400">
                    只删「只属于这块板」的集;别的板也在用的不会被删掉。
                  </p>
                </div>
              )}

              {delPlan.questions.length > 0 && (
                <div>
                  <div className={labelCls}>将一并删除的问题（{delPlan.questions.length}）</div>
                  <div className="mt-1 max-h-32 overflow-y-auto rounded-xl border border-slate-200 divide-y divide-slate-50">
                    {delPlan.questions.map((q) => (
                      <div key={q.id} className="flex justify-between gap-2 px-3 py-1.5 text-xs text-slate-600">
                        <span className="truncate">{q.title}</span>
                        <span className="shrink-0 text-slate-300">{q.scenic_id || '集团级'}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="flex justify-end gap-2 pt-1">
                <Button variant="outline" onClick={() => setDelPlan(null)} disabled={deleting}>取消</Button>
                <Button className="bg-red-600 hover:bg-red-700" onClick={doDelete} disabled={deleting}>
                  {deleting && <Loader2 size={14} className="animate-spin mr-1" />}
                  确认删除（含 {Object.values(delPlan.counts).reduce((a, b) => a + b, 0)} 项关联）
                </Button>
              </div>
            </div>
          </Modal>
        ) : (
          <ConfirmModal title="删除数据资产" danger confirmLabel="删除" loading={deleting}
            message={`「${delPlan.asset.name}」下面没有关联的数据集或问题,确定删除?`}
            onCancel={() => setDelPlan(null)} onConfirm={doDelete} />
        )
      )}
    </div>
  );
};

export default Assets;
