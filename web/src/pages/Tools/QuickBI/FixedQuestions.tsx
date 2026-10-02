import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Edit3, ListChecks, Loader2, Play, Plus, RefreshCw, Rocket, Square, Trash2, Search, Zap } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import { Badge } from '../../../components/ui/badge';
import { Modal, ConfirmModal } from '../../../components/ui/PageHelpers';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import ResultView, { CAP_COLOR, emptyLive, reduceQbi, type QbiLive } from './ResultView';
import { CAPABILITY_OPTIONS, ConnectionSelect, ScenicSelect, TargetPicker, describeTarget, inputCls, labelCls, selectCls } from './shared';
import PublishToolModal from './PublishToolModal';
import DimensionLabelPicker from './DimensionLabelPicker';
import { canDeleteRow, canEditRow, useCapabilities, whyDisabled } from '../../../lib/capabilities';
import ThemeIssuePanel from './ThemeIssuePanel';

// 请求类别:与 Quick BI 的四个产品叫法对齐,外部调用方按它拉清单
export const ASK_TYPE_OPTIONS = [
  { key: 'qa', label: '问数' },
  { key: 'insight', label: '解读' },
  { key: 'dashboard', label: '报表' },
  { key: 'report', label: '报告' },
];
const askTypeOfCapability = (c: string) =>
  c === 'insight' ? 'insight' : c === 'report' ? 'report' : c === 'dashboard_qa' ? 'dashboard' : 'qa';

const varsOf = (q: string) => Array.from(new Set(Array.from(q.matchAll(/\{\{\s*(\w+)\s*\}\}/g)).map((m) => m[1])));

const FixedQuestions: React.FC<{ connections: api.QbiConnection[]; refreshKey: number }> = ({ connections, refreshKey }) => {
  const [items, setItems] = useState<api.QbiFixedQuestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [category, setCategory] = useState('');
  const [domain, setDomain] = useState('');
  const [askType, setAskType] = useState('');
  // 数据域候选 = 预置 8 个 + 现场已经用过的;允许直接填新值,所以是 datalist 而不是纯下拉
  const [domainOptions, setDomainOptions] = useState<string[]>([]);
  // 三级标签树(主题→维度→指标)。问题挂在指标下面,APP 前台按它组织问题树 ——
  // 没挂标签的问题在前台没有挂载点,建了也看不到,所以这里既能选、也能筛。
  const [labelTree, setLabelTree] = useState<api.DimensionLabelTopic[]>([]);
  const [onlyUnlabeled, setOnlyUnlabeled] = useState(false);
  // 按景区筛:同一套连接下不同景区各有各的问题,不筛的话列表很快就混成一片
  const [scenicFilter, setScenicFilter] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [editing, setEditing] = useState<Partial<api.QbiFixedQuestion> | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState<api.QbiFixedQuestion | null>(null);
  const [params, setParams] = useState<Record<string, string>>({});
  const [live, setLive] = useState<QbiLive | null>(null);
  const [batch, setBatch] = useState<any[] | null>(null);
  const [batching, setBatching] = useState(false);
  const [deleting, setDeleting] = useState<api.QbiFixedQuestion | null>(null);
  // 角色 × 景区的判定统一来自后端,按钮的可点性和后端真正放行的范围是同一套
  const cap = useCapabilities();
  const [publishing, setPublishing] = useState<api.QbiFixedQuestion | null>(null);
  // 正在加速的那条问题的 id。一条一条来 —— 加速是真的去跑一遍,
  // 一排按钮连点下去就是同时往 Quick BI 打十几个请求
  const [prewarming, setPrewarming] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      setItems((await api.qbiListFixed({
        keyword: keyword || undefined, category: category || undefined,
        domain: domain || undefined, ask_type: askType || undefined,
        unlabeled: onlyUnlabeled || undefined,
        scenic_id: scenicFilter || undefined,
      })).items);
    } catch (e: any) { toast.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [refreshKey, category, domain, askType, onlyUnlabeled, scenicFilter]);
  useEffect(() => { api.qbiListDomains().then((r) => setDomainOptions(r.items)).catch(() => {}); }, []);
  useEffect(() => { api.getDimensionLabelTree().then((r) => setLabelTree(r.topics)).catch(() => {}); }, []);

  const categories = useMemo(() => Array.from(new Set(items.map((i) => i.category).filter(Boolean))) as string[], [items]);
  const connName = (id: string) => connections.find((c) => c.id === id)?.name || id.slice(0, 8);
  // 列表里显示景区名而不是一串 PFTSC… 编号:按当前这批问题用到的景区 ID 拉一次主数据
  const [scenicNames, setScenicNames] = useState<Record<string, string>>({});
  const scenicName = (id: string) => scenicNames[id] || id;
  useEffect(() => {
    const ids = Array.from(new Set(items.map((i) => i.scenic_id).filter(Boolean))) as string[];
    const missing = ids.filter((id) => !scenicNames[id]);
    if (!missing.length) return;
    Promise.all(missing.map((id) => api.getScenicCatalogDetail(id).then(
      (r) => [id, r.scenic_name] as const).catch(() => [id, id] as const)))
      .then((pairs) => setScenicNames((prev) => ({ ...prev, ...Object.fromEntries(pairs) })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  const save = async () => {
    if (!editing) return;
    setSaving(true);
    try {
      await api.qbiSaveFixed(editing, editing.id);
      toast.success('已保存');
      setEditing(null);
      load();
    } catch (e: any) { toast.error(e.message); }
    finally { setSaving(false); }
  };

  const startRun = (q: api.QbiFixedQuestion) => {
    setRunning(q);
    setParams({});
    setLive(null);
    if (varsOf(q.question).length === 0) execute(q, {});
  };

  const execute = async (q: api.QbiFixedQuestion, p: Record<string, string>) => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let state = emptyLive(q.question);
    setLive(state);
    try {
      await api.qbiFixedStream(q.id, p, (e) => { state = reduceQbi(state, e); setLive(state); }, ctrl.signal);
    } catch (e: any) {
      if (e.name !== 'AbortError') { state = { ...state, streaming: false, errors: [...state.errors, { message: e.message }] }; setLive(state); }
    } finally {
      if (state.streaming) setLive({ ...state, streaming: false });
      load();
    }
  };

  // 「加速」:立刻重新跑一遍,把结果刷成新的缓存。
  // 之后这段「存储时间」内,同一景区问同一条问题的人直接读这一份,不必现跑。
  const prewarm = async (q: api.QbiFixedQuestion) => {
    setPrewarming(q.id);
    try {
      const r = await api.qbiPrewarmFixed(q.id);
      if (r.ok) {
        toast.success(`已加速:${(r.duration_ms / 1000).toFixed(1)}s,${r.prewarm_ttl_minutes} 分钟内直接回放`);
      } else if (r.status === 'running') {
        // 报告类要跑几分钟,这里不等它 —— 收尾由后台的报告扫描接手
        toast.success('报告已提交生成,完成后自动成为缓存');
      } else {
        toast.error(`加速失败:${r.error || r.status}`);
      }
      load();
    } catch (e: any) { toast.error(e.message); }
    finally { setPrewarming(null); }
  };

  const runBatch = async () => {
    setBatching(true);
    setBatch([]);
    try { setBatch((await api.qbiBatchRun(selected)).items); load(); }
    catch (e: any) { toast.error(e.message); setBatch(null); }
    finally { setBatching(false); }
  };

  const vars = running ? varsOf(running.question) : [];

  return (
    <div className="space-y-4">
      {/* 主题解读单独一块:它是 APP 首页那 5 张卡片的后台配置,
          和运营自己配的指标问题不是一回事,混在同一张表里没人找得到 */}
      <ThemeIssuePanel />
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-64">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input className={cn(inputCls, 'pl-8')} placeholder="搜索标题 / 问题" value={keyword}
            onChange={(e) => setKeyword(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && load()} />
        </div>
        <select className={cn(selectCls, 'w-36')} value={domain} onChange={(e) => setDomain(e.target.value)}>
          <option value="">全部数据域</option>
          {domainOptions.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <select className={cn(selectCls, 'w-32')} value={askType} onChange={(e) => setAskType(e.target.value)}>
          <option value="">全部类别</option>
          {ASK_TYPE_OPTIONS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
        </select>
        <select className={cn(selectCls, 'w-36')} value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">全部分类</option>
          {categories.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <div className="w-52">
          <ScenicSelect registeredOnly placeholder="全部景区" value={scenicFilter}
            onChange={setScenicFilter} />
        </div>
        <label className="flex items-center gap-1.5 text-xs text-slate-600">
          <input type="checkbox" checked={onlyUnlabeled} onChange={(e) => setOnlyUnlabeled(e.target.checked)} />
          只看未挂标签
        </label>
        <Button variant="outline" onClick={load} className="gap-1.5"><RefreshCw size={14} />刷新</Button>
        <div className="flex-1" />
        <Button variant="outline" disabled={!selected.length || batching} onClick={runBatch} className="gap-1.5">
          {batching ? <Loader2 size={14} className="animate-spin" /> : <ListChecks size={14} />}批量执行({selected.length})
        </Button>
        <Button className="gap-1.5 bg-[#2563eb] text-white"
          disabled={!connections.length || !cap.can_create}
          title={!cap.can_create ? whyDisabled(cap, cap.scope_scenic_id, '修改') : undefined}
          onClick={() => setEditing({ capability: 'dataset_qa', ask_type: 'qa', target: {}, active: true,
            visibility: 'group',
            // 景区角色只能建在自己景区下,直接预填,省得存的时候被后端打回来
            scenic_id: cap.manage_all || cap.is_group ? undefined : (cap.scope_scenic_id || undefined),
            connection_id: (connections.find((c) => c.is_default) || connections[0])?.id })}>
          <Plus size={14} />新建固定问题
        </Button>
      </div>

      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs text-slate-500">
            <tr>
              <th className="w-8 px-3 py-2"><input type="checkbox" checked={items.length > 0 && selected.length === items.length}
                onChange={(e) => setSelected(e.target.checked ? items.map((i) => i.id) : [])} /></th>
              <th className="px-3 py-2">问题</th><th className="px-3 py-2">主题 / 维度 / 指标</th>
              <th className="px-3 py-2">数据域</th>
              <th className="px-3 py-2">类别 / 能力</th><th className="px-3 py-2">目标数据</th>
              <th className="px-3 py-2">最近运行</th><th className="px-3 py-2 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={8} className="py-10 text-center"><Loader2 className="mx-auto animate-spin text-slate-300" /></td></tr>}
            {!loading && items.length === 0 && (
              <tr><td colSpan={8} className="py-10 text-center text-xs text-slate-400">暂无固定问题。可在「数据资产」里从仪表板一键生成。</td></tr>
            )}
            {!loading && items.map((q) => (
              <tr key={q.id} className={cn('border-t border-slate-100', !q.active && 'opacity-50')}>
                <td className="px-3 py-2"><input type="checkbox" checked={selected.includes(q.id)}
                  onChange={(e) => setSelected(e.target.checked ? [...selected, q.id] : selected.filter((x) => x !== q.id))} /></td>
                <td className="max-w-md px-3 py-2">
                  <div className="font-medium text-slate-800">{q.title}</div>
                  <div className="truncate text-xs text-slate-400">{q.question}</div>
                  <div className="text-[10px] text-slate-300">
                    {q.category ? `${q.category} · ` : ''}{connName(q.connection_id)}
                    {' · '}{q.scenic_id ? (scenicName(q.scenic_id)) : '集团级'}
                  </div>
                </td>
                <td className="px-3 py-2">
                  {q.metric_id ? (
                    <div className="text-xs text-slate-600">
                      {q.topic} / {q.dimension}
                      <div className="font-semibold text-slate-700">{q.metric}</div>
                    </div>
                  ) : (
                    // 这一条在 APP 的问题树上没有挂载点 —— 最常见的「后台配了、前台没有」
                    <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-600">未挂标签 · APP 不展示</span>
                  )}
                </td>
                <td className="px-3 py-2">
                  {q.domain
                    ? <Badge className="border border-slate-200 bg-slate-50 text-slate-600">{q.domain}</Badge>
                    : <span className="text-xs text-slate-300">未分域</span>}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-col gap-1">
                    <span className="text-xs font-semibold text-slate-600">{q.ask_type_label || '问数'}</span>
                    <Badge className={cn('border', CAP_COLOR[q.capability])}>{q.capability_label}</Badge>
                  </div>
                </td>
                <td className="px-3 py-2 text-xs text-slate-500">
                  {q.target_data_name || describeTarget(q.target)}
                </td>
                <td className="px-3 py-2 text-xs">
                  {q.last_run_status ? (
                    <span className={cn(q.last_run_status === 'success' ? 'text-emerald-600' : q.last_run_status === 'failed' ? 'text-rose-500' : 'text-blue-500')}>
                      {({ success: '成功', failed: '失败', running: '生成中' } as any)[q.last_run_status] || q.last_run_status}
                    </span>
                  ) : <span className="text-slate-300">—</span>}
                  {q.last_run_at && <div className="text-[10px] text-slate-300">{new Date(q.last_run_at).toLocaleString()}</div>}
                  {/* 开了加速的行标出来:否则「为什么这条秒回、那条要转十秒」只能靠猜 */}
                  {q.prewarm_enabled && (
                    <div className="mt-0.5 inline-flex items-center gap-0.5 rounded bg-amber-50 px-1 py-0.5 text-[10px] text-amber-600">
                      <Zap size={9} />加速 {q.prewarm_interval_minutes || 30}/{q.prewarm_ttl_minutes || 60} 分
                    </div>
                  )}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right">
                  {/* 加速:只在「已启用 + 已开加速」的问题上出现。
                      停用的问题不预热(预热了也没人看得到),没开加速的问题按钮点了也不会有缓存,
                      与其让人点完发现没变化,不如不给这个按钮 —— 去编辑里开开关。 */}
                  {q.active && q.prewarm_enabled && (
                    <Button size="sm" variant="ghost" className="gap-1 text-amber-600"
                      disabled={prewarming !== null}
                      title={`立刻重新跑一遍并刷新缓存(刷新 ${q.prewarm_interval_minutes || 30} 分钟 / 存储 ${q.prewarm_ttl_minutes || 60} 分钟)`}
                      onClick={() => prewarm(q)}>
                      {prewarming === q.id ? <Loader2 size={12} className="animate-spin" /> : <Zap size={12} />}加速
                    </Button>
                  )}
                  <Button size="sm" variant="outline" className="gap-1" onClick={() => startRun(q)}><Play size={12} />运行</Button>
                  <Button size="sm" variant="ghost" title="生成工具" onClick={() => setPublishing(q)}><Rocket size={12} /></Button>
                  <Button size="sm" variant="ghost" disabled={!canEditRow(cap, q.scenic_id)}
                    title={canEditRow(cap, q.scenic_id) ? '编辑' : whyDisabled(cap, q.scenic_id, '修改')}
                    onClick={() => setEditing(q)}><Edit3 size={12} /></Button>
                  <Button size="sm" variant="ghost" className="text-rose-500" disabled={!canDeleteRow(cap, q.scenic_id)}
                    title={canDeleteRow(cap, q.scenic_id) ? '删除' : whyDisabled(cap, q.scenic_id, '删除')}
                    onClick={() => setDeleting(q)}><Trash2 size={12} /></Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editing && (
        <Modal title={editing.id ? '编辑固定问题' : '新建固定问题'} wide onClose={() => setEditing(null)}
          footer={<>
            <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
            <Button className="bg-[#2563eb]" disabled={saving || !editing.question || !editing.connection_id} onClick={save}>
              {saving && <Loader2 size={14} className="mr-1 animate-spin" />}保存
            </Button>
          </>}>
          <div className="grid grid-cols-2 gap-3">
            <div><label className={labelCls}>连接</label>
              <ConnectionSelect items={connections} value={editing.connection_id || ''}
                onChange={(v) => setEditing({ ...editing, connection_id: v, target: {} })} /></div>
            <div><label className={labelCls}>能力</label>
              <select className={selectCls} value={editing.capability}
                onChange={(e) => setEditing({ ...editing, capability: e.target.value as api.QbiCapability, target: {},
                                              ask_type: editing.ask_type || askTypeOfCapability(e.target.value) })}>
                {CAPABILITY_OPTIONS.filter((o) => o.key !== 'auto').map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
              </select></div>
            <div><label className={labelCls}>数据域</label>
              {/* 用 input + datalist:8 个预置值随手可选,现场要加新域直接打字就行,不用改代码 */}
              <input className={inputCls} list="qbi-domain-options" placeholder="票务 / 财务 / 舆情 …(可填新值)"
                value={editing.domain || ''} onChange={(e) => setEditing({ ...editing, domain: e.target.value })} />
              <datalist id="qbi-domain-options">
                {domainOptions.map((d) => <option key={d} value={d} />)}
              </datalist></div>
            <div><label className={labelCls}>请求类别</label>
              <select className={selectCls} value={editing.ask_type || askTypeOfCapability(editing.capability || '')}
                onChange={(e) => setEditing({ ...editing, ask_type: e.target.value })}>
                {ASK_TYPE_OPTIONS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
              </select>
              <p className="mt-1 text-[11px] text-slate-400">外部调用按这个类别拉该景区的问题清单;不改就按能力自动推导。</p></div>
            <div className="col-span-2"><label className={labelCls}>标题</label>
              <input className={inputCls} value={editing.title || ''} onChange={(e) => setEditing({ ...editing, title: e.target.value })} placeholder="不填默认取问题前 40 字" /></div>
            <div className="col-span-2"><label className={labelCls}>问题 *(支持 {'{{变量}}'},运行时填写)</label>
              <textarea className="min-h-[72px] w-full rounded-lg border border-slate-200 p-2 text-sm outline-none focus:border-blue-400"
                value={editing.question || ''} onChange={(e) => setEditing({ ...editing, question: e.target.value })}
                placeholder="如:{{month}} 各景区客流 TOP10 及环比变化" /></div>
            <div className="col-span-2"><label className={labelCls}>目标数据</label>
              <TargetPicker connectionId={editing.connection_id || ''} capability={editing.capability || 'dataset_qa'}
                scenicId={editing.scenic_id || null}
                value={editing.target || {}} onChange={(t) => setEditing({ ...editing, target: t })}
                // 选了仪表板就把能力纠正成「报表 · 仪表板」——不纠正的话后端仍按数据集解析目标,
                // 看板白选了而且不报错;请求类别跟着能力推导,一并同步
                onCapabilityChange={(c) => setEditing((e) => (e ? {
                  ...e, capability: c as api.QbiCapability, ask_type: askTypeOfCapability(c) } : e))} /></div>
            {/* 三级标签:APP 端的问题树按 主题→维度→指标 组织,问题挂在指标下面。
                三级都不选也能存(管理端仍可用、可批量执行),但前台看不到 —— 所以这里明说。 */}
            {/* 口径说明:按钮上的问题要短(「【主题解读】客群」),而模型需要的恰恰是那些
                啰嗦的约束。分成两个字段,发起时口径**追加**在问题后面,不替换用户原话。 */}
            <div className="col-span-2">
              <label className={labelCls}>口径说明(发给模型的约束,不展示给用户)</label>
              <textarea className={cn(inputCls, 'h-20 py-2')} value={editing.model_instruction || ''}
                onChange={(e) => setEditing({ ...editing, model_instruction: e.target.value })}
                placeholder="如:围绕客群结构(年龄、性别、客源地、出游距离、消费偏好),先给结论再给数据,指出结构变化与风险,最后给 2-3 条可执行建议" />
              <p className="mt-1 text-[11px] text-slate-400">
                留空则只把上面那句问题原样发过去 —— 问题越短,模型越只能靠猜。解读/报告类尤其要写。
              </p>
            </div>
            <div className="col-span-2">
              <label className={labelCls}>主题 / 维度 / 指标(APP 问题树的挂载点)</label>
              <DimensionLabelPicker tree={labelTree} value={editing}
                onChange={(v) => setEditing({ ...editing, ...v })} />
            </div>
            <div><label className={labelCls}>分类</label>
              <input className={inputCls} value={editing.category || ''} onChange={(e) => setEditing({ ...editing, category: e.target.value })} /></div>
            <div><label className={labelCls}>可见范围</label>
              <select className={selectCls} value={editing.visibility || 'group'} onChange={(e) => setEditing({ ...editing, visibility: e.target.value })}>
                <option value="private">private</option><option value="scenic">scenic</option>
                <option value="group">group</option><option value="public">public(景区 API Key 也可调用)</option>
              </select></div>
            <div className="col-span-2">
              <label className={labelCls}>归属景区(留空 = 集团级,全景区通用)</label>
              {/* 从景区主数据里挑,不手填 ID:绑错了的表现只是「那个景区看不到这条问题」,
                  没有报错,也没人会往「景区 ID 敲错一位」上想。
                  景区角色只能建在自己景区下,这里锁死 —— 能改的话存的时候一样会被后端打回来。 */}
              <ScenicSelect value={editing.scenic_id || ''} registeredOnly
                disabled={!cap.manage_all && !cap.is_group}
                onChange={(v) => setEditing({ ...editing, scenic_id: v })} />
              {!cap.manage_all && !cap.is_group && (
                <p className="mt-1 text-[11px] text-slate-400">景区角色只能维护本景区({cap.scope_scenic_id || '未绑定'})的问题</p>
              )}
            </div>
            {/* 加速(预热):后台提前把结果跑好,前台/APP 再问同一景区的同一条问题直接回放。
                两个分钟数留空 = 跟随全局默认,所以 placeholder 写的是默认值而不是填死 0 —— 
                填死了之后改全局默认就影响不到这条问题了。 */}
            <div className="col-span-2 rounded-xl border border-amber-100 bg-amber-50/40 p-3">
              <label className="flex items-center gap-2 text-xs font-semibold text-slate-700">
                <input type="checkbox" checked={!!editing.prewarm_enabled}
                  onChange={(e) => setEditing({ ...editing, prewarm_enabled: e.target.checked })} />
                开启加速(预热)
              </label>
              <p className="mt-1 text-[11px] text-slate-500">
                后台每隔「刷新时间」提前跑好这条问题;「存储时间」内,前台或 APP 问**同一景区的同一条问题**
                直接回放已跑好的结果,不必现跑。不同景区各存各的,不会串。
              </p>
              {editing.prewarm_enabled && (
                <div className="mt-2 grid grid-cols-2 gap-3">
                  <div><label className={labelCls}>刷新时间(分钟)</label>
                    <input type="number" min={1} max={10080} className={inputCls} placeholder="留空 = 默认 30"
                      value={editing.prewarm_interval_minutes ?? ''}
                      onChange={(e) => setEditing({ ...editing,
                        prewarm_interval_minutes: e.target.value === '' ? null : Number(e.target.value) })} /></div>
                  <div><label className={labelCls}>存储时间(分钟)</label>
                    <input type="number" min={1} max={10080} className={inputCls} placeholder="留空 = 默认 60"
                      value={editing.prewarm_ttl_minutes ?? ''}
                      onChange={(e) => setEditing({ ...editing,
                        prewarm_ttl_minutes: e.target.value === '' ? null : Number(e.target.value) })} /></div>
                  <p className="col-span-2 text-[11px] text-slate-400">
                    存储时间要 ≥ 刷新时间,否则两次预热之间会出现没有缓存的空窗(开了加速却没变快,而且不报错)。
                    {editing.capability === 'report' && ' 报告类一条要跑几分钟,刷新间隔建议放宽到几小时。'}
                  </p>
                </div>
              )}
            </div>
            <div><label className={labelCls}>排序</label>
              <input type="number" className={inputCls} value={editing.sort_order ?? 0}
                onChange={(e) => setEditing({ ...editing, sort_order: Number(e.target.value) })} /></div>
            <label className="flex items-center gap-2 pt-5 text-xs text-slate-600">
              <input type="checkbox" checked={editing.active !== false} onChange={(e) => setEditing({ ...editing, active: e.target.checked })} />启用
            </label>
          </div>
        </Modal>
      )}

      {running && (
        <Modal title={`运行:${running.title}`} wide onClose={() => { abortRef.current?.abort(); setRunning(null); }}
          footer={live?.streaming
            ? <Button variant="outline" className="gap-1" onClick={() => abortRef.current?.abort()}><Square size={12} />停止</Button>
            : vars.length > 0 ? <Button className="bg-[#2563eb]" disabled={vars.some((v) => !params[v])}
                onClick={() => execute(running, params)}><Play size={14} className="mr-1" />执行</Button> : undefined}>
          <div className="rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-700">{running.question}</div>
          {vars.length > 0 && (
            <div className="grid grid-cols-2 gap-2">
              {vars.map((v) => (
                <div key={v}><label className={labelCls}>{v}</label>
                  <input className={inputCls} value={params[v] || ''} onChange={(e) => setParams({ ...params, [v]: e.target.value })} /></div>
              ))}
            </div>
          )}
          {live && <ResultView live={live} />}
        </Modal>
      )}

      {batch && (
        <Modal title="批量执行结果" wide onClose={() => setBatch(null)}>
          {batching && <div className="flex items-center gap-2 text-sm text-slate-500"><Loader2 size={14} className="animate-spin" />串行执行中…(报告类只创建任务)</div>}
          {batch.map((r, i) => {
            const q = items.find((x) => x.id === r.fixed_question_id);
            return (
              <div key={i} className="rounded-xl border border-slate-100 p-3 text-xs">
                <div className="mb-1 flex items-center justify-between">
                  <span className="font-semibold text-slate-700">{q?.title || r.fixed_question_id}</span>
                  <span className={r.status === 'success' ? 'text-emerald-600' : r.status === 'running' ? 'text-blue-500' : 'text-rose-500'}>
                    {r.status}{r.duration_ms ? ` · ${(r.duration_ms / 1000).toFixed(1)}s` : ''}</span>
                </div>
                <div className="line-clamp-4 whitespace-pre-wrap text-slate-500">{r.error || r.answer || (r.chart_count ? `返回 ${r.chart_count} 张图表` : '')}</div>
              </div>
            );
          })}
        </Modal>
      )}

      {publishing && (
        <PublishToolModal connections={connections} onClose={() => setPublishing(null)}
          initial={{ name: `qbi_${publishing.title}`.slice(0, 60), connection_id: publishing.connection_id,
            capability: publishing.capability, fixed_question_id: publishing.id, description: publishing.title }} />
      )}

      {deleting && (
        <ConfirmModal title="删除固定问题" danger confirmLabel="删除" message={`确定删除「${deleting.title}」?`}
          onCancel={() => setDeleting(null)}
          onConfirm={async () => {
            try { await api.qbiDeleteFixed(deleting.id); toast.success('已删除'); load(); } catch (e: any) { toast.error(e.message); }
            setDeleting(null);
          }} />
      )}
    </div>
  );
};

export default FixedQuestions;
