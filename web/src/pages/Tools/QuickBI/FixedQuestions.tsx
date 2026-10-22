import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Brain, Copy, Download, Edit3, Eye, ListChecks, Loader2, MoreHorizontal, Play, Plus, RefreshCw, Rocket, Save, Square, Trash2, Search, Zap } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '../../../components/ui/button';
import { Badge } from '../../../components/ui/badge';
import { Modal, ConfirmModal } from '../../../components/ui/PageHelpers';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import ResultView, { CAP_COLOR, emptyLive, reduceQbi, type QbiLive } from './ResultView';
import { BatchConfirmModal, BatchCopyModal, BatchIntervalModal, BatchLogModal, BatchOpMenu, OP_ABOUT, OP_LABEL, type BatchLogLine, type BatchOpKey } from './BatchOps';
import { CAPABILITY_OPTIONS, ConnectionSelect, ScenicSelect, SearchableSelect, TargetPicker, describeTarget, inputCls, labelCls, selectCls } from './shared';
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
// 运行时刻由后端填的变量(见 service.render_runtime_vars):不该让人在运行弹窗里手填
const RUNTIME_VARS = new Set(['截止时间', '统计日期', '昨日']);
const userVarsOf = (q: string) => varsOf(q).filter((v) => !RUNTIME_VARS.has(v));

/** 筛选控件统一长相:「维度名:下拉」。
 *
 * 维度名只写在左边这一次 —— 塞进选项里(「全部数据域」「最近运行:全部」)会让同一个词
 * 在一行里出现两遍,下拉本来就不宽,全被维度名吃掉。选项一律是「全部 / 具体取值」。
 * 宽度统一,宁可截断(悬停看全名)也不让控件随内容长短参差不齐。
 */
const FilterSelect: React.FC<{
  label: string; value: string; onChange: (v: string) => void;
  options: { value: string; label: string }[]; title?: string;
}> = ({ label, value, onChange, options, title }) => (
  <label className="flex items-center gap-1.5 text-xs font-medium text-slate-700" title={title}>
    <span className="shrink-0">{label}：</span>
    {/* 没选具体取值时是浅灰占位色,选了才是正文色;option 显式回正,免得展开后整列发灰 */}
    <select value={value} onChange={(e) => onChange(e.target.value)}
      className={cn(selectCls, 'w-36', !value && 'text-slate-400')}>
      <option value="" className="text-slate-700">全部</option>
      {options.map((o) => (
        <option key={o.value} value={o.value} title={o.label} className="text-slate-700">{o.label}</option>
      ))}
    </select>
  </label>
);

/** 列表里就地改一个布尔列的开关。做成真开关而不是复选框:勾与不勾的差别太小,扫一列容易看串行。 */
const OnOffSwitch: React.FC<{ on: boolean; disabled?: boolean; title?: string; onChange: (v: boolean) => void }> =
  ({ on, disabled, title, onChange }) => (
    <button type="button" role="switch" aria-checked={on} disabled={disabled} title={title}
      onClick={() => onChange(!on)}
      className={cn('relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors',
        on ? 'bg-emerald-500' : 'bg-slate-300', disabled ? 'cursor-not-allowed opacity-50' : 'hover:opacity-90')}>
      <span className={cn('inline-block h-4 w-4 rounded-full bg-white shadow transition-transform',
        on ? 'translate-x-[18px]' : 'translate-x-[2px]')} />
    </button>
  );

// ── 行内「更多操作」菜单(挂到 body) ──────────────────────────────
// 必须 portal:整张表的卡片容器是 overflow-hidden(给圆角修边),搜索后只剩一行时
// 行内下拉必然越过底边被裁掉 —— 表现为「点了更多却什么都没出来」。挂到 body 上
// 顺带拿到「下方放不下翻到上方、贴边内收」。
const RowMenu: React.FC<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
}> = ({ open, onOpenChange, children }) => {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const a = anchorRef.current?.getBoundingClientRect();
      if (!a) return;
      const menu = menuRef.current;
      const h = menu?.offsetHeight || 0;
      const w = menu?.offsetWidth || 160;
      const below = a.bottom + 4;
      const top = below + h > window.innerHeight - 8 ? Math.max(8, a.top - h - 4) : below;
      setPos({ top, left: Math.max(8, Math.min(a.right - w, window.innerWidth - w - 8)) });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const el = e.target as Node;
      if (anchorRef.current?.contains(el) || menuRef.current?.contains(el)) return;
      onOpenChange(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onOpenChange(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, onOpenChange]);

  return (
    <div className="relative" onClick={(e) => e.stopPropagation()}>
      <span ref={anchorRef} className="inline-flex">
        <Button size="sm" variant="ghost" title="更多操作" onClick={() => onOpenChange(!open)}>
          <MoreHorizontal size={14} />
        </Button>
      </span>
      {open && createPortal(
        <div ref={menuRef} style={{ top: pos?.top ?? 0, left: pos?.left ?? 0, opacity: pos ? 1 : 0 }}
          className="fixed z-50 w-40 overflow-hidden rounded-lg border border-slate-200 bg-white py-1 shadow-lg">
          {children}
        </div>,
        document.body,
      )}
    </div>
  );
};
const menuItemCls = 'flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] text-slate-700 hover:bg-slate-50 disabled:opacity-50';

const LAST_STATUS_LABEL: Record<string, string> = { success: '成功', failed: '失败', running: '生成中' };

const FixedQuestions: React.FC<{ connections: api.QbiConnection[]; refreshKey: number; hasAssetsTab?: boolean }> =
  ({ connections, refreshKey, hasAssetsTab = true }) => {
  const [items, setItems] = useState<api.QbiFixedQuestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [category, setCategory] = useState('');
  const [domain, setDomain] = useState('');
  const [askType, setAskType] = useState('');
  // 连接筛选:取数凭证、数据集、仪表板全都挂在连接上 —— 运营要对的常常是「这批问题跑在哪个账号下」
  const [conn, setConn] = useState('');
  const [prewarmFilter, setPrewarmFilter] = useState('');
  // 最近一次运行结果:'' = 全部,success/failed/running 按值筛,none = 从没跑过的
  const [lastStatus, setLastStatus] = useState('');
  // 数据域候选 = 预置 + 现场已经用过的;允许直接填新值,所以是 datalist 而不是纯下拉
  const [domainOptions, setDomainOptions] = useState<string[]>([]);
  // 三级标签树(主题→维度→指标)。问题挂在指标下面,APP 前台按它组织问题树 ——
  // 没挂标签的问题在前台没有挂载点,建了也看不到,所以这里既能选、也能筛。
  const [labelTree, setLabelTree] = useState<api.DimensionLabelTopic[]>([]);
  const [labelFilter, setLabelFilter] = useState('');
  // APP 主题(三级标签第一级,= APP 主题卡分组;「主题解读」就是主题类锚点行)
  const [topicFilter, setTopicFilter] = useState('');
  // 口径说明库:导入 = 从库里抄一份到本问题的口径说明;存库 = 把当前口径存成库里一条
  const [pickingPrompt, setPickingPrompt] = useState(false);
  const [savingPrompt, setSavingPrompt] = useState(false);
  const [prompts, setPrompts] = useState<api.QbiPrompt[]>([]);
  const [promptKw, setPromptKw] = useState('');
  const [promptName, setPromptName] = useState('');
  // 按景区筛:同一套连接下不同景区各有各的问题,不筛的话列表很快就混成一片
  const [scenicFilter, setScenicFilter] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [editing, setEditing] = useState<Partial<api.QbiFixedQuestion> | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState<api.QbiFixedQuestion | null>(null);
  const [params, setParams] = useState<Record<string, string>>({});
  const [live, setLive] = useState<QbiLive | null>(null);
  const [deleting, setDeleting] = useState<api.QbiFixedQuestion | null>(null);
  // 角色 × 景区的判定统一来自后端,按钮的可点性和后端真正放行的范围是同一套
  const cap = useCapabilities();
  const [publishing, setPublishing] = useState<api.QbiFixedQuestion | null>(null);
  // 正在加速的那条问题的 id。一条一条来 —— 加速是真的去跑一遍,
  // 一排按钮连点下去就是同时往 Quick BI 打十几个请求
  const [prewarming, setPrewarming] = useState<string | null>(null);
  // 「更多操作」下拉展开在哪一行。高频的常驻(编辑 / 运行 / 查看思考),低频的收进下拉
  const [openMenuFor, setOpenMenuFor] = useState('');
  // 正在切「是否上架」的行:按下到落库之间禁止再点,连点两次会觉得自己改了又改回去
  const [switching, setSwitching] = useState('');
  // 查看思考:该问题最近一次成功的「解读/报告类」运行的思考过程
  const [thinking, setThinking] = useState<{ title: string; text: string; at: string | null; meta: string } | null>(null);
  const [loadingThinking, setLoadingThinking] = useState('');
  // 批量:后端逐条回事件、前端滚成日志;改时间 / 复制要先收参数,所以另有小弹窗
  const [batchLines, setBatchLines] = useState<BatchLogLine[]>([]);
  const [batchRunning, setBatchRunning] = useState(false);
  const [batchStat, setBatchStat] = useState<{ total: number; succeeded: number; failed: number } | null>(null);
  const [batchOp, setBatchOp] = useState<BatchOpKey | null>(null);
  const [batchAsk, setBatchAsk] = useState<'set_interval' | 'copy' | null>(null);
  // 所有批量动作执行前都先落到这里做二次确认 —— 批量一次动几十条,不确认就动手代价太大
  const [pendingBatch, setPendingBatch] = useState<{
    op: BatchOpKey;
    extra: { interval_minutes?: number; ttl_minutes?: number; prewarm_enabled?: boolean; connection_id?: string };
    summary?: string[];
  } | null>(null);
  // 任务队列水位:APP 预热与 APP / 开放平台 / 操练台共用同一条队,这是「这条队现在堵不堵」的读数
  const [queue, setQueue] = useState<api.QbiQueueStats | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      setItems((await api.qbiListFixed({
        keyword: keyword || undefined, category: category || undefined,
        domain: domain || undefined, ask_type: askType || undefined,
        connection_id: conn || undefined,
        unlabeled: labelFilter === 'none' || undefined,
        topic_id: topicFilter || undefined,
        scenic_id: scenicFilter || undefined,
        prewarm: prewarmFilter === '' ? undefined : prewarmFilter === '1',
        last_status: lastStatus || undefined,
      })).items);
    } catch (e: any) { toast.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ },
    [refreshKey, category, domain, askType, conn, labelFilter, topicFilter, scenicFilter, prewarmFilter, lastStatus]);
  useEffect(() => { api.qbiListDomains().then((r) => setDomainOptions(r.items)).catch(() => {}); }, []);
  useEffect(() => { api.getDimensionLabelTree().then((r) => setLabelTree(r.topics)).catch(() => {}); }, []);
  // 选中的行被筛掉 / 删掉之后,批量操作不该还带着它们
  useEffect(() => { setSelected((s) => s.filter((id) => items.some((i) => i.id === id))); }, [items]);

  const loadQueue = async () => {
    try { setQueue(await api.qbiQueueStats()); } catch { /* 水位拿不到不影响列表本身 */ }
  };
  // 队列水位 30s 自动刷:派单与出队都是后台在动,静态数字没有意义(点击也能立刻刷)
  useEffect(() => {
    loadQueue();
    const t = setInterval(loadQueue, 30000);
    return () => clearInterval(t);
  }, []);
  const ageOf = (s: number) =>
    s < 60 ? `${s} 秒` : s < 3600 ? `${Math.floor(s / 60)} 分钟` : `${Math.floor(s / 3600)} 小时`;

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

  // 能不能改 / 删以后端给的 can_write / can_delete 为准(与 save/delete 鉴权同一口径),
  // 老接口没给时再退回前端按角色 × 景区推算
  const canWrite = (q: Partial<api.QbiFixedQuestion>) => q.can_write ?? canEditRow(cap, q.scenic_id);
  const canDelete = (q: api.QbiFixedQuestion) => q.can_delete ?? canDeleteRow(cap, q.scenic_id);
  // 只读视图:与其让人改完才知道改不了,不如一进弹窗就是只读
  const readonly = !!editing?.id && !canWrite(editing);
  const ro = readonly ? { disabled: true } : {};

  const save = async () => {
    if (!editing || readonly) return;
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
    if (userVarsOf(q.question).length === 0) execute(q, {});
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
    // 没开加速的行不请求后端,直接说清楚原因 —— 菜单项常驻就是为了让这个原因能被看见
    if (!q.prewarm_enabled || !q.active) {
      toast.error(!q.active ? '这条问题已下架,下架的问题不预热' : '这条问题没开加速 —— 先在编辑里打开「开启加速」');
      return;
    }
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

  // 查看思考:取最近一次**成功的解读/报告类**运行的思考过程。不能拿运行记录列表取 ——
  // 列表不带 result(太大),翻出来永远是空框;问数类的 thinking 是 NL2SQL 取数日志,
  // 也不该当思考展示。这两条过滤都放在后端 latest_thinking。
  const openThinking = async (q: api.QbiFixedQuestion) => {
    setLoadingThinking(q.id);
    try {
      const r = await api.qbiLatestThinking(q.id);
      setThinking({
        title: q.title, at: r.created_at || null,
        // 标明是哪一次运行、什么能力、跑了多久 —— 改过口径之后要对着它核对,不至于认错版本
        meta: r.found ? [r.capability_label || r.capability,
          r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : '',
          r.run_id ? `运行 ${r.run_id.slice(0, 8)}` : ''].filter(Boolean).join(' · ') : '',
        text: r.text || r.reason || '',
      });
    } catch (e: any) { toast.error(e.message); }
    finally { setLoadingThinking(''); }
  };

  // 上架/下架就地拨:进编辑弹窗改一个开关要「打开→翻到底→勾→保存」四步,
  // 而这偏偏是最常被临时拨动的一列。先本地翻转再发请求,服务端没认就退回去。
  const toggleActive = async (q: api.QbiFixedQuestion, next: boolean) => {
    const prev = items;
    setSwitching(q.id);
    setItems((list) => list.map((x) => (x.id === q.id ? { ...x, active: next } : x)));
    try {
      await api.qbiSaveFixed({ active: next }, q.id);
      toast.success(next ? '已上架' : '已下架');
    } catch (e: any) {
      setItems(prev);
      toast.error(e.message);
    } finally {
      setSwitching('');
    }
  };

  // ── 口径说明库 ──
  const loadPrompts = async (kw?: string) => {
    try { setPrompts((await api.qbiListPrompts(kw || undefined)).items); }
    catch (e: any) { toast.error(e.message); }
  };
  // 导入 = 把库里那条的内容**抄**进本问题的口径说明,不是引用:
  // 之后库里再改不会动这条正在跑的问题(口径要先在一条上验证再推广)
  const importPrompt = (p: api.QbiPrompt) => {
    if (!editing) return;
    setEditing({ ...editing, model_instruction: p.content });
    setPickingPrompt(false);
    toast.success(`已导入口径「${p.name}」`);
  };
  const savePromptToLibrary = async () => {
    if (!editing?.model_instruction?.trim()) return;
    try {
      await api.qbiSavePrompt({ name: promptName.trim() || `${editing.title || '未命名'} · 口径`,
        content: editing.model_instruction.trim() });
      toast.success('已存入口径库');
      setSavingPrompt(false);
      setPromptName('');
    } catch (e: any) { toast.error(e.message); }
  };
  const removePrompt = async (p: api.QbiPrompt) => {
    try { await api.qbiDeletePrompt(p.id); toast.success('已删除'); loadPrompts(promptKw || undefined); }
    catch (e: any) { toast.error(e.message); }
  };

  // ── 批量 ──
  const now = () => new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const pushLine = (l: BatchLogLine) => setBatchLines((ls) => [...ls, { at: now(), ...l }]);

  const startBatch = async (op: BatchOpKey, extra: NonNullable<typeof pendingBatch>['extra'] = {}) => {
    setBatchOp(op);
    setBatchLines([]);
    setBatchStat(null);
    setBatchRunning(true);
    try {
      if (op === 'run') {
        // 批量执行是同步接口(串行跑完一起回),回来后逐条落进同一个日志弹窗
        pushLine({ text: `开始串行执行 ${selected.length} 条(报告类只创建任务)` });
        const r = await api.qbiBatchRun(selected);
        let ok = 0;
        r.items.forEach((it: any, i: number) => {
          const good = it.status === 'success' || it.status === 'running';
          if (good) ok += 1;
          const title = items.find((x) => x.id === it.fixed_question_id)?.title || it.fixed_question_id;
          pushLine({ ok: good, text: `[${i + 1}/${r.items.length}] ${title} · ${it.status}`
            + `${it.duration_ms ? ` · ${(it.duration_ms / 1000).toFixed(1)}s` : ''}${it.error ? ` · ${it.error}` : ''}` });
        });
        setBatchStat({ total: r.items.length, succeeded: ok, failed: r.items.length - ok });
      } else {
        await api.qbiBatchOpStream({ op, ids: selected, ...extra }, (e) => {
          if (e.type === 'start') pushLine({ text: `开始处理 ${e.total} 条${e.ignored ? `(超过上限,忽略 ${e.ignored} 条)` : ''}` });
          else if (e.type === 'item') pushLine({
            ok: e.ok,
            text: `[${e.index}/${e.total}] ${e.title || e.id || ''} · ${e.ok ? '成功' : '失败'}${e.message ? ` · ${e.message}` : ''}`,
          });
          else if (e.type === 'done') {
            setBatchStat({ total: e.total || 0, succeeded: e.succeeded || 0, failed: e.failed || 0 });
            pushLine({ text: `完成:成功 ${e.succeeded} / 失败 ${e.failed}(共 ${e.total})`, ok: !e.failed });
          }
        });
      }
    } catch (e: any) {
      if (e.name !== 'AbortError') pushLine({ ok: false, text: `请求失败:${e.message}` });
    } finally {
      setBatchRunning(false);
      load();     // 批量改完不刷,列表还是旧值,会让人以为没生效
    }
  };

  const pickBatchOp = (op: BatchOpKey) => {
    // 改时间 / 复制要先收参数;收完参数一样要过二次确认
    if (op === 'set_interval' || op === 'copy') { setBatchAsk(op); return; }
    setPendingBatch({ op, extra: {} });
  };

  const vars = running ? userVarsOf(running.question) : [];
  const labelPath = (q: api.QbiFixedQuestion) => [q.topic, q.dimension, q.metric].filter(Boolean).join(' / ');

  return (
    <div className="space-y-4">
      {/* 主题解读单独一块:它是 APP 首页那 5 张卡片的后台配置,
          和运营自己配的指标问题不是一回事,混在同一张表里没人找得到 */}
      <ThemeIssuePanel />

      {/* 两行工具条:第一行 = 搜索 + 对当前列表做点什么(队列水位、批量);
          第二行 = 全部筛选 + 新建入口。控件间距写死,不随窗口伸缩,两行才看得齐。 */}
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-[320px] flex-1">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input className={cn(inputCls, 'pl-8')} placeholder="搜索标题 / 问题 / 问题ID" value={keyword}
              onChange={(e) => setKeyword(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && load()} />
          </div>
          {/* 「搜索」而不是「刷新」:它按关键词 + 下面那排筛选一起重拉列表。给主色:是这一行的主动作 */}
          <Button onClick={load} className="shrink-0 gap-1.5 bg-[#2563eb] text-white">
            <RefreshCw size={14} />搜索
          </Button>
          {/* 队列水位:排队多、或队首等太久时转黄 —— 这两种情况都意味着实时问答正在被拖慢 */}
          <button type="button" onClick={loadQueue}
            className={cn('inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium transition',
              queue && (queue.queued > 200 || queue.oldest_queued_seconds > 600)
                ? 'border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100'
                : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50')}
            title={
              '任务队列水位(全局同一条队:APP 预热与 APP、开放平台、操练台共用)\n' +
              (queue
                ? `排队 ${queue.queued} · 运行中 ${queue.running}\n` +
                  `其中预热:排队 ${queue.prewarm.queued} · 运行中 ${queue.prewarm.running}\n` +
                  `其余:排队 ${queue.others.queued} · 运行中 ${queue.others.running}\n` +
                  `队首那条已等 ${ageOf(queue.oldest_queued_seconds)}`
                : '读取中…') +
              '\n点击立即刷新(每 30 秒自动刷新)'}>
            <ListChecks size={13} />
            {queue ? `队列 排队 ${queue.queued} · 运行中 ${queue.running}` : '队列 读取中…'}
          </button>
          <BatchOpMenu count={selected.length} busy={batchRunning} onPick={pickBatchOp} />
        </div>

        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <FilterSelect label="数据域" value={domain} onChange={setDomain}
            options={domainOptions.map((d) => ({ value: d, label: d }))}
            title="只是一枚归类标签:不参与取数,用于筛选与对外目录" />
          <FilterSelect label="请求类别" value={askType} onChange={setAskType}
            options={ASK_TYPE_OPTIONS.map((o) => ({ value: o.key, label: o.label }))} />
          {/* 连接几十条时普通下拉挑不动(名字又长又相近),用能按名称搜索的下拉 */}
          <label className="flex items-center gap-1.5 text-xs font-medium text-slate-700"
            title="按问题用的连接筛:取数凭证、数据集、仪表板都挂在连接上">
            <span className="shrink-0">连接：</span>
            <SearchableSelect className="w-40" value={conn} onChange={setConn}
              options={connections.map((c) => ({ value: c.id, label: c.name }))}
              searchPlaceholder="搜索连接名称" />
          </label>
          <label className="flex items-center gap-1.5 text-xs font-medium text-slate-700">
            <span className="shrink-0">景区：</span>
            <div className="w-44">
              <ScenicSelect registeredOnly placeholder="全部" value={scenicFilter} onChange={setScenicFilter} />
            </div>
          </label>
          <FilterSelect label="加速" value={prewarmFilter} onChange={setPrewarmFilter}
            options={[{ value: '1', label: '已开加速' }, { value: '0', label: '未开加速' }]} />
          <FilterSelect label="最近运行" value={lastStatus} onChange={setLastStatus}
            options={[{ value: 'success', label: '成功' }, { value: 'failed', label: '失败' },
              { value: 'running', label: '生成中' }, { value: 'none', label: '从未运行' }]}
            title="按最近一次运行结果筛;「从未运行」= 配完还没跑过的" />
          <FilterSelect label="APP 主题" value={topicFilter} onChange={setTopicFilter}
            options={labelTree.map((t) => ({ value: t.topic_id, label: t.topic }))}
            title="按 APP 主题卡(三级标签第一级)筛;「主题解读」= 首页主题卡片的解读问题" />
          <FilterSelect label="APP 标签" value={labelFilter} onChange={setLabelFilter}
            options={[{ value: 'none', label: '未挂标签' }]}
            title="没挂三级标签的问题在 APP 的问题树上没有挂载点,前台看不到" />
          {categories.length > 0 && (
            <FilterSelect label="分类" value={category} onChange={setCategory}
              options={categories.map((c) => ({ value: c, label: c }))} />
          )}
          {/* 新建归到筛选这一行末尾:它只是入口,不抢「搜索」的视觉。
              一条连接都没有时不给建 —— 连接是必填项,建出来的问题必然跑不出数 */}
          <Button variant="outline" className="gap-1.5"
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
      </div>

      <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs text-slate-500">
            <tr>
              <th className="w-8 px-3 py-2"><input type="checkbox" checked={items.length > 0 && selected.length === items.length}
                onChange={(e) => setSelected(e.target.checked ? items.map((i) => i.id) : [])} /></th>
              <th className="px-3 py-2">问题ID</th>
              {/* 问题列固定宽:标题 + 问题 + 连接信息都在这里,不封顶时一条长问题就能把整表
                  撑到横向滚动。标题与问题各最多两行,读不全的悬停看 title 或进编辑看全文。 */}
              <th className="w-[250px] px-3 py-2">问题</th>
              <th className="px-3 py-2" title="数据域只是一枚归类标签;下面一行是 APP 问题树的挂载点(主题 / 维度 / 指标)">数据域 / APP 标签</th>
              <th className="px-3 py-2" title="类别由能力自动推导,下面是实际的数据来源">请求类别</th>
              <th className="w-[12rem] px-3 py-2">目标数据</th>
              <th className="px-3 py-2">最近运行</th>
              <th className="px-3 py-2" title="下架 = 整条停用:管理端执行会报「已停用」,APP 与对外开放目录也拿不到这条">是否上架</th>
              <th className="px-3 py-2 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={9} className="py-10 text-center"><Loader2 className="mx-auto animate-spin text-slate-300" /></td></tr>}
            {!loading && items.length === 0 && (
              <tr><td colSpan={9} className="py-10 text-center text-xs text-slate-400">
                暂无固定问题。{hasAssetsTab ? '可在「数据资产」里从仪表板一键生成。' : '请联系集团管理员配置。'}
              </td></tr>
            )}
            {!loading && items.map((q) => (
              <tr key={q.id} className={cn('border-t border-slate-100', !q.active && 'opacity-50')}>
                <td className="px-3 py-2"><input type="checkbox" checked={selected.includes(q.id)}
                  onChange={(e) => setSelected(e.target.checked ? [...selected, q.id] : selected.filter((x) => x !== q.id))} /></td>
                {/* 外部调用(开放平台 fixed_question_id)与 APP 都按 id 定位,
                    运营要能一眼看到并复制;搜索框也支持按 id 查 */}
                <td className="px-3 py-2">
                  <div className="flex items-center gap-1">
                    <code className="max-w-[8.5rem] truncate font-mono text-[11px] text-slate-500" title={q.id}>{q.id}</code>
                    <button type="button" title="复制问题ID" className="shrink-0 text-slate-400 hover:text-blue-600"
                      onClick={() => { navigator.clipboard?.writeText(q.id); toast.success('已复制问题ID'); }}>
                      <Copy size={12} />
                    </button>
                  </div>
                </td>
                <td className="w-[250px] px-3 py-2">
                  <div className="line-clamp-2 font-medium text-slate-800" title={q.title}>{q.title}</div>
                  <div className="line-clamp-2 text-xs text-slate-400" title={q.question}>{q.question}</div>
                  {/* 有没有写口径要在列表里看得见:两条标题一样的问题,差的往往就是那半段口径 */}
                  {!!q.model_instruction?.trim() && (
                    <div className="mt-0.5 text-[10px] text-indigo-500">含口径说明({q.model_instruction.trim().length} 字)</div>
                  )}
                  <div className="text-[10px] text-slate-300">
                    {connName(q.connection_id)}{' · '}{q.scenic_id ? scenicName(q.scenic_id) : '集团级'}
                    {/* 开了加速就把「多久一次」一并写出来:只写两个字看不出是每小时还是每天 */}
                    {q.prewarm_enabled && (
                      <span className="ml-1 text-amber-600">加速 · {q.prewarm_interval_minutes || 30}/{q.prewarm_ttl_minutes || 60} 分</span>
                    )}
                  </div>
                </td>
                <td className="px-3 py-2">
                  {q.domain
                    ? <Badge className="border border-slate-200 bg-slate-50 text-slate-600">{q.domain}</Badge>
                    : <span className="text-xs text-slate-300">未分域</span>}
                  {/* APP 标签紧跟着列出来:两个归类要一起核对,只看见一个容易漏配 */}
                  {q.metric_id
                    ? <div className="mt-0.5 max-w-[11rem] truncate text-[11px] text-slate-400" title={labelPath(q)}>APP:{labelPath(q)}</div>
                    // 这一条在 APP 的问题树上没有挂载点 —— 最常见的「后台配了、前台没有」
                    : <div className="mt-0.5 text-[11px] text-amber-600">未挂标签 · APP 不展示</div>}
                </td>
                <td className="px-3 py-2">
                  <div className="flex flex-col gap-1">
                    {/* 上行粗类别,下行补能力细节。能力名和类别名多数同名(问数 / 报告…),
                        同名时下行改显示实际的数据来源 —— 那才是它比类别多出来的信息 */}
                    <span className="text-xs font-semibold text-slate-600">{q.ask_type_label || '问数'}</span>
                    <Badge className={cn('border', CAP_COLOR[q.capability])}
                      title={q.capability_label ? `能力:${q.capability_label}` : undefined}>
                      {q.capability_label && q.capability_label !== q.ask_type_label
                        ? q.capability_label
                        : (q.capability_source || q.capability_label || q.capability)}
                    </Badge>
                  </div>
                </td>
                <td className="px-3 py-2 text-xs text-slate-500">
                  {/* 宽度钉在列上、内容截断(完整值走 title):多份数据集时这一行能长到几百字 */}
                  <div className="max-w-[12rem] truncate" title={q.target_data_name || describeTarget(q.target)}>
                    {q.target_data_name || describeTarget(q.target)}
                  </div>
                </td>
                <td className="px-3 py-2 text-xs">
                  {q.last_run_status ? (
                    <span className={cn(q.last_run_status === 'success' ? 'text-emerald-600' : q.last_run_status === 'failed' ? 'text-rose-500' : 'text-blue-500')}>
                      {LAST_STATUS_LABEL[q.last_run_status] || q.last_run_status}
                    </span>
                  ) : <span className="text-slate-300">—</span>}
                  {q.last_run_at && <div className="text-[10px] text-slate-300">{new Date(q.last_run_at).toLocaleString()}</div>}
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    {/* 没有修改权限的行灰掉并给出原因:拨得动才让人以为能改,点了报错比一开始就不可点更难受 */}
                    <OnOffSwitch on={q.active !== false} disabled={!canWrite(q) || switching === q.id}
                      title={!canWrite(q) ? whyDisabled(cap, q.scenic_id, '修改')
                        : q.active !== false ? '点一下下架(APP 端随之消失)' : '点一下上架'}
                      onChange={(v) => toggleActive(q, v)} />
                    <span className={cn('whitespace-nowrap text-[11px]', q.active === false ? 'text-rose-500' : 'text-emerald-600')}>
                      {q.active === false ? '已下架' : '已上架'}
                    </span>
                  </div>
                </td>
                {/* 操作列:高频三个常驻(编辑 / 运行 / 查看思考),低频的收进「更多」,
                    删除是唯一不可逆的动作,压在菜单最后一位。一律图标 + 文字。 */}
                <td className="whitespace-nowrap px-3 py-2 text-right">
                  <div className="flex items-center justify-end gap-1">
                    <Button size="sm" variant="ghost" className="gap-0.5"
                      title={canWrite(q) ? '编辑' : `查看(${whyDisabled(cap, q.scenic_id, '修改')})`} onClick={() => setEditing(q)}>
                      {canWrite(q) ? <Edit3 size={12} /> : <Eye size={12} />}
                      <span className="text-[11px]">{canWrite(q) ? '编辑' : '查看'}</span>
                    </Button>
                    <Button size="sm" variant="outline" className="gap-1" onClick={() => startRun(q)}><Play size={12} />运行</Button>
                    <Button size="sm" variant="ghost" className="gap-0.5" disabled={!!loadingThinking}
                      title={'查看最近一次成功的「解读 / 报告」类运行的思考过程。\n'
                        + '问数类运行的 thinking 是 NL2SQL 取数日志,不是解读的思考,已按能力排除。'}
                      onClick={() => openThinking(q)}>
                      {loadingThinking === q.id ? <Loader2 size={12} className="animate-spin" /> : <Brain size={12} />}
                      <span className="text-[11px]">查看思考</span>
                    </Button>
                    <RowMenu open={openMenuFor === q.id} onOpenChange={(v) => setOpenMenuFor(v ? q.id : '')}>
                      {/* 单条加速留在下拉第一位:只想刷一条时,不该先去勾选再开批量菜单。
                          不按开关隐藏:藏起来就查不到「这条为什么没有加速」,点了再说明原因更直白 */}
                      <button type="button" className={menuItemCls} disabled={prewarming !== null}
                        title={`立刻重新跑一遍并刷新缓存(刷新 ${q.prewarm_interval_minutes || 30} 分钟 / 存储 ${q.prewarm_ttl_minutes || 60} 分钟)`}
                        onClick={() => { setOpenMenuFor(''); prewarm(q); }}>
                        {prewarming === q.id ? <Loader2 size={12} className="animate-spin" /> : <Zap size={12} />}立即加速
                      </button>
                      <button type="button" className={menuItemCls} title="把这条问题生成一个 quickbi 工具,供 Skill / Agent 绑定"
                        onClick={() => { setOpenMenuFor(''); setPublishing(q); }}>
                        <Rocket size={12} />生成工具
                      </button>
                      {canDelete(q) && (
                        <button type="button"
                          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] text-rose-600 hover:bg-rose-50"
                          onClick={() => { setOpenMenuFor(''); setDeleting(q); }}>
                          <Trash2 size={12} />删除
                        </button>
                      )}
                    </RowMenu>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editing && (
        <Modal title={readonly ? '查看固定问题' : editing.id ? '编辑固定问题' : '新建固定问题'} wide onClose={() => setEditing(null)}
          footer={readonly
            ? <Button variant="outline" onClick={() => setEditing(null)}>关闭</Button>
            : <>
              <Button variant="outline" onClick={() => setEditing(null)}>取消</Button>
              <Button className="bg-[#2563eb]" disabled={saving || !editing.question || !editing.connection_id} onClick={save}>
                {saving && <Loader2 size={14} className="mr-1 animate-spin" />}保存
              </Button>
            </>}>
          {readonly && (
            <div className="mb-3 rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
              只读:{whyDisabled(cap, editing.scenic_id, '修改')}
            </div>
          )}
          {/* fieldset disabled:只读时整张表单一次性锁住,不用每个控件各写一遍 */}
          <fieldset {...ro} className="grid grid-cols-2 gap-3">
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
              {/* 用 input + datalist:预置值随手可选,现场要加新域直接打字就行,不用改代码 */}
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
            <div className="col-span-2"><label className={labelCls}>问题 *(支持 {'{{变量}}'},运行时填写;{'{{截止时间}} {{统计日期}} {{昨日}}'} 由系统按运行时刻自动填)</label>
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
            {/* 口径说明:按钮上的问题要短(「【主题解读】客群」),而模型需要的恰恰是那些
                啰嗦的约束。分成两个字段,发起时口径**追加**在问题后面,不替换用户原话。 */}
            <div className="col-span-2">
              <div className="mb-1 flex items-center justify-between gap-2">
                <label className={cn(labelCls, 'mb-0')}>口径说明(发给模型的约束,不展示给用户)</label>
                {!readonly && (
                  <div className="flex items-center gap-1">
                    <button type="button" className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] text-blue-600 hover:bg-blue-50"
                      onClick={() => { setPromptKw(''); loadPrompts(); setPickingPrompt(true); }}>
                      <Download size={12} />从口径库导入
                    </button>
                    <button type="button" disabled={!editing.model_instruction?.trim()}
                      title={editing.model_instruction?.trim() ? '把当前口径存成库里一条,给别的问题复用' : '先填写口径说明才能存库'}
                      className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                      onClick={() => { setPromptName(''); setSavingPrompt(true); }}>
                      <Save size={12} />存入口径库
                    </button>
                  </div>
                )}
              </div>
              <textarea className={cn(inputCls, 'h-20 py-2')} value={editing.model_instruction || ''}
                onChange={(e) => setEditing({ ...editing, model_instruction: e.target.value })}
                placeholder="如:围绕客群结构(年龄、性别、客源地、出游距离、消费偏好),先给结论再给数据,指出结构变化与风险,最后给 2-3 条可执行建议" />
              <p className="mt-1 text-[11px] text-slate-400">
                留空则只把上面那句问题原样发过去 —— 问题越短,模型越只能靠猜。解读/报告类尤其要写。
              </p>
            </div>
            {/* 三级标签:APP 端的问题树按 主题→维度→指标 组织,问题挂在指标下面。
                三级都不选也能存(管理端仍可用、可批量执行),但前台看不到 —— 所以这里明说。 */}
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
                  没有报错,也没人会往「景区 ID 敲错一位」上想。景区角色只能建在自己景区下,锁死。 */}
              <ScenicSelect value={editing.scenic_id || ''} registeredOnly
                disabled={readonly || (!cap.manage_all && !cap.is_group)}
                onChange={(v) => setEditing({ ...editing, scenic_id: v })} />
              {!cap.manage_all && !cap.is_group && (
                <p className="mt-1 text-[11px] text-slate-400">景区角色只能维护本景区({cap.scope_scenic_id || '未绑定'})的问题</p>
              )}
            </div>
            {/* 加速(预热):后台提前把结果跑好,前台/APP 再问同一景区的同一条问题直接回放。
                两个分钟数留空 = 跟随全局默认 —— 填死了之后改全局默认就影响不到这条问题了。 */}
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
              <input type="checkbox" checked={editing.active !== false} onChange={(e) => setEditing({ ...editing, active: e.target.checked })} />上架(启用)
            </label>
          </fieldset>
        </Modal>
      )}

      {running && (
        <Modal title={`运行:${running.title}`} wide onClose={() => { abortRef.current?.abort(); setRunning(null); }}
          footer={live?.streaming
            ? <Button variant="outline" className="gap-1" onClick={() => abortRef.current?.abort()}><Square size={12} />停止</Button>
            : vars.length > 0 ? <Button className="bg-[#2563eb]" disabled={vars.some((v) => !params[v])}
                onClick={() => execute(running, params)}><Play size={14} className="mr-1" />执行</Button> : undefined}>
          {/* 展示问题 + 口径,运营核对的就是「到底发了什么」 */}
          <div className="max-h-40 overflow-auto whitespace-pre-wrap rounded-xl bg-slate-50 px-3 py-2 text-sm text-slate-700">
            {running.question}
            {running.model_instruction?.trim() && (
              <div className="mt-1 text-xs text-slate-400">口径要求:{running.model_instruction.trim()}</div>
            )}
          </div>
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

      {/* 口径库导入:挑一条抄进本问题的口径说明 */}
      {pickingPrompt && (
        <Modal title="从口径库导入" wide onClose={() => setPickingPrompt(false)}>
          <div className="relative mb-3">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input className={cn(inputCls, 'pl-8')} placeholder="搜索名称 / 内容(回车)" value={promptKw}
              onChange={(e) => setPromptKw(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && loadPrompts(promptKw)} />
          </div>
          <div className="max-h-[60vh] space-y-2 overflow-auto">
            {prompts.length === 0 && <div className="py-8 text-center text-xs text-slate-400">库里还没有口径。在编辑问题时点「存入口径库」即可新增。</div>}
            {prompts.map((p) => (
              <div key={p.id} className="rounded-xl border border-slate-200 p-3">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-700">{p.name}</span>
                  {p.description && <span className="truncate text-[11px] text-slate-400">{p.description}</span>}
                  <div className="flex-1" />
                  {p.can_write !== false && (
                    <Button size="sm" variant="ghost" className="text-rose-500" title="从库中删除(已导入的问题不受影响)"
                      onClick={() => removePrompt(p)}><Trash2 size={12} /></Button>
                  )}
                  <Button size="sm" className="gap-1 bg-[#2563eb] text-white" onClick={() => importPrompt(p)}>
                    <Download size={12} />导入
                  </Button>
                </div>
                <div className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 px-2 py-1 text-xs text-slate-500">
                  {p.content}
                </div>
              </div>
            ))}
          </div>
          <p className="mt-2 text-[11px] text-slate-400">
            导入 = 把内容抄进这条问题的口径说明(之后改库不影响这条);已经填了内容会被替换。
          </p>
        </Modal>
      )}

      {savingPrompt && editing && (
        <Modal title="存入口径库" onClose={() => setSavingPrompt(false)}
          footer={<>
            <Button variant="outline" onClick={() => setSavingPrompt(false)}>取消</Button>
            <Button className="bg-[#2563eb]" onClick={savePromptToLibrary}>存入</Button>
          </>}>
          <div className="space-y-3">
            <div><label className={labelCls}>名称</label>
              <input className={inputCls} value={promptName} onChange={(e) => setPromptName(e.target.value)}
                placeholder={editing.title ? `${editing.title} · 口径` : '客流同比口径'} /></div>
            <div><label className={labelCls}>内容(存的就是当前这条问题的口径说明)</label>
              <div className="max-h-40 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
                {editing.model_instruction}
              </div></div>
            <p className="text-[11px] text-slate-400">
              存的是当前文本的快照:以后改这条问题的口径不会动库里那条,反之也一样。
            </p>
          </div>
        </Modal>
      )}

      {thinking && (
        <Modal title={`思考过程:${thinking.title}`} wide onClose={() => setThinking(null)}>
          {/* 思考过程按「运行」存:标明是哪一次运行的,免得改了问题之后对着旧过程核对 */}
          <div className="mb-2 text-[11px] text-slate-400">
            {thinking.at
              ? `解读类运行:${new Date(thinking.at).toLocaleString()}${thinking.meta ? ` · ${thinking.meta}` : ''}`
              : (thinking.meta || '暂无运行记录')}
          </div>
          <div className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-xl bg-slate-50 px-3 py-2 text-sm leading-relaxed text-slate-700">
            {thinking.text}
          </div>
        </Modal>
      )}

      {/* 批量日志:点过批量操作就常驻(跑完也不自动关)—— 关掉就看不到哪条失败了 */}
      {(batchRunning || batchStat || batchLines.length > 0) && (
        <BatchLogModal title={batchOp ? OP_LABEL[batchOp] : '批量操作'} lines={batchLines} running={batchRunning}
          total={batchStat?.total} succeeded={batchStat?.succeeded} failed={batchStat?.failed}
          about={batchOp ? OP_ABOUT[batchOp] : undefined}
          onClose={() => { setBatchLines([]); setBatchStat(null); }} />
      )}

      {batchAsk === 'set_interval' && (
        <BatchIntervalModal onCancel={() => setBatchAsk(null)}
          onConfirm={(iv, ttl, enabled) => {
            setBatchAsk(null);
            setPendingBatch({
              op: 'set_interval',
              extra: enabled === false ? { prewarm_enabled: false }
                : { interval_minutes: iv, ttl_minutes: ttl ?? undefined, prewarm_enabled: enabled ?? undefined },
              summary: enabled === false ? ['关闭加速']
                : [`刷新时间 → ${iv} 分钟;存储时间 → ${ttl ? `${ttl} 分钟` : '原值(不足刷新时间时抬到刷新时间)'}`
                  + (enabled ? '(一并开启加速)' : '(开关不动)')],
            });
          }} />
      )}

      {batchAsk === 'copy' && (
        <BatchCopyModal connections={connections} onCancel={() => setBatchAsk(null)}
          onConfirm={(connId) => {
            setBatchAsk(null);
            const c = connections.find((x) => x.id === connId);
            setPendingBatch({ op: 'copy', extra: { connection_id: connId },
              summary: [`目标连接:${c?.name || connId}`, `归属景区:跟随该连接(${c?.scenic_id || '集团级'})`] });
          }} />
      )}

      {pendingBatch && (
        <BatchConfirmModal op={pendingBatch.op} count={selected.length} summary={pendingBatch.summary}
          onCancel={() => setPendingBatch(null)}
          onConfirm={() => { const p = pendingBatch; setPendingBatch(null); startBatch(p.op, p.extra); }} />
      )}

      {publishing && (
        <PublishToolModal connections={connections} onClose={() => setPublishing(null)}
          initial={{ name: `qbi_${publishing.title}`.slice(0, 60), connection_id: publishing.connection_id,
            capability: publishing.capability, fixed_question_id: publishing.id, description: publishing.title }} />
      )}

      {deleting && (
        <ConfirmModal title="删除固定问题" danger confirmLabel="删除" message={`确定删除「${deleting.title}」?删除后不可恢复。`}
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
