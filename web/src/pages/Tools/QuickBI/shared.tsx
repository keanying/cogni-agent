import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, ClipboardList, Database, FileSpreadsheet, LayoutDashboard, Loader2, Sparkles, Upload, X } from 'lucide-react';
import { toast } from 'sonner';
import * as api from '../../../lib/api';
import { ChartScopePicker } from './ChartScopePicker';
import { DatasetMultiPicker } from './DatasetMultiPicker';
import { cn } from '../../../lib/utils';

export const selectCls = 'h-10 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm outline-none focus:border-blue-400';
export const inputCls = 'h-10 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm outline-none focus:border-blue-400';
export const labelCls = 'mb-1 block text-[11px] font-bold uppercase tracking-wide text-slate-500';

export const CAPABILITY_OPTIONS: { key: string; label: string; hint: string }[] = [
  // 这是**选择器**,同为「问数」的两项要能分清打在哪儿,所以带上来源;
  // 结果徽标只显示四个字面(见 ResultView 的 CAP_LABEL)。
  { key: 'auto', label: '自动路由', hint: '按问题意图:含「报告」→ 报告;含「解读/趋势/异常」→ 解读;否则问数' },
  { key: 'dataset_qa', label: '问数 · 数据集', hint: '对数据集提问,不选数据集时自动智能选表' },
  { key: 'dashboard_qa', label: '报表 · 仪表板', hint: '基于已有仪表板提问,在仪表板用到的数据集里选表' },
  { key: 'file_qa', label: '问数 · 上传文件', hint: 'xls/xlsx/csv ≤5MB' },
  { key: 'insight', label: '解读', hint: '仪表板快照 / 上传文件的趋势、异常、归因解读' },
  { key: 'report', label: '报告', hint: '异步生成分析报告,可引用上传文件与仪表板数据' },
];

export function useConnections() {
  const [items, setItems] = useState<api.QbiConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const load = async () => {
    setLoading(true);
    try { setItems((await api.qbiListConnections()).items); }
    catch (e: any) { toast.error(e.message || '加载连接失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);
  return { items, loading, reload: load };
}

export const ConnectionSelect: React.FC<{
  items: api.QbiConnection[]; value: string; onChange: (v: string) => void; className?: string;
}> = ({ items, value, onChange, className }) => (
  <select value={value} onChange={(e) => onChange(e.target.value)} className={cn(selectCls, className)}>
    {items.length === 0 && <option value="">(暂无连接,请先到「连接配置」新建)</option>}
    {items.map((c) => (
      <option key={c.id} value={c.id}>
        {c.name}{c.is_default ? '(默认)' : ''}{c.virtual ? '(环境变量)' : ''}{c.status === 'failed' ? ' ⚠' : ''}
      </option>
    ))}
  </select>
);

type Source = 'auto' | 'dataset' | 'dashboard' | 'file' | 'data';

// 每种能力允许打在哪些目标数据上。
//
// 关键的一条:**报表 / 解读 / 报告都能打在仪表板上,也都能打在数据集上** ——
// 原先 dataset_qa 只给「不指定 / 数据集」,于是运营把请求类别选成「报告」之后,
// 目标数据那一排仍然没有「仪表板」可选,只能建一条打在数据集上的报告。
// 操练台一直是能选仪表板的,两边口径不一致,运营自然觉得是后台少做了一块。
const SOURCES: Record<string, Source[]> = {
  auto: ['auto', 'dataset', 'dashboard', 'file'],
  dataset_qa: ['auto', 'dataset', 'dashboard'],
  dashboard_qa: ['auto', 'dashboard', 'dataset'],
  file_qa: ['file'],
  insight: ['dashboard', 'dataset', 'file', 'data'],
  report: ['auto', 'dashboard', 'dataset', 'file'],
};

/** 选了这种目标数据之后,能力应当是哪一个(与后端 route_capability 同口径)。
 *
 *  选了仪表板却还挂着「问数·数据集」的话,后端会按数据集去解析目标,拿不到就智能选表 ——
 *  看板白选了,而且不报错。所以这里选完目标数据顺手把能力纠正过来。 */
export function capabilityForSource(capability: string, src: Source): string {
  if (capability === 'auto' || capability === 'report' || capability === 'insight') return capability;
  if (src === 'dashboard') return 'dashboard_qa';
  if (src === 'dataset') return 'dataset_qa';
  if (src === 'file') return 'file_qa';
  return capability;
}
const SOURCE_META: Record<Source, { label: string; icon: any }> = {
  auto: { label: '不指定', icon: Sparkles },
  dataset: { label: '数据集', icon: Database },
  dashboard: { label: '仪表板', icon: LayoutDashboard },
  file: { label: '上传文件', icon: FileSpreadsheet },
  data: { label: '粘贴数据', icon: ClipboardList },
};

function sourceOf(t: Record<string, any>): Source {
  if (t.cube_id || t.cube_name || (t.cube_ids || []).length) return 'dataset';
  if (t.asset_id || t.page_id || t.dashboard) return 'dashboard';
  if (t.file_id || (t.file_ids || []).length) return 'file';
  if (t.data) return 'data';
  return 'auto';
}

/** 目标数据选择器:数据集 / 已登记仪表板(或直接贴地址) / 上传文件 / 粘贴数据。 */
export const TargetPicker: React.FC<{
  connectionId: string; capability: string; value: Record<string, any>; onChange: (v: Record<string, any>) => void;
  /** 选了目标数据之后能力该跟着变(仪表板→报表、数据集→问数);不传就不纠正 */
  onCapabilityChange?: (c: string) => void;
  /** 只列这个景区(+集团级)的资产 —— 资产是按景区登记的,列别的景区的等于选了也用不了 */
  scenicId?: string | null;
}> = ({ connectionId, capability, value, onChange, onCapabilityChange, scenicId }) => {
  const allowed = SOURCES[capability] || SOURCES.auto;
  const [source, setSource] = useState<Source>(() => sourceOf(value));
  const [cubes, setCubes] = useState<{ cube_id: string; cube_name: string }[] | null>(null);
  const [cubeLoading, setCubeLoading] = useState(false);
  const [assets, setAssets] = useState<api.QbiAsset[]>([]);
  const [files, setFiles] = useState<api.QbiFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!allowed.includes(source)) { setSource(allowed[0]); onChange({}); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capability]);

  useEffect(() => {
    setCubes(null);
    if (!connectionId) return;
    api.qbiListAssets({ connection_id: connectionId, scenic_id: scenicId || undefined })
      .then((r) => setAssets(r.items)).catch(() => setAssets([]));
    api.qbiListFiles(connectionId).then((r) => setFiles(r.items)).catch(() => setFiles([]));
  }, [connectionId, scenicId]);

  const loadCubes = async () => {
    if (!connectionId || cubes) return;
    setCubeLoading(true);
    try { setCubes((await api.qbiListCubes(connectionId)).items); }
    catch (e: any) { toast.error(e.message || '拉取数据集失败'); setCubes([]); }
    finally { setCubeLoading(false); }
  };
  useEffect(() => { if (source === 'dataset') loadCubes(); /* eslint-disable-next-line */ }, [source, connectionId]);

  const upload = async (f: File) => {
    setUploading(true);
    try {
      const r = await api.qbiUploadFile(f, connectionId);
      setFiles((prev) => [r, ...prev]);
      onChange(capability === 'report' ? { file_ids: [r.id] } : { file_id: r.id });
      (r.warnings || []).forEach((w) => toast.warning(w));
      toast.success(`已上传「${r.file_name}」`);
    } catch (e: any) { toast.error(e.message || '上传失败'); }
    finally { setUploading(false); }
  };

  const dashboards = assets.filter((a) => a.kind === 'dashboard');
  const datasetAssets = assets.filter((a) => a.kind === 'dataset');
  const reportFiles: string[] = value.file_ids || (value.file_id ? [value.file_id] : []);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {allowed.map((s) => {
          const M = SOURCE_META[s];
          return (
            <button key={s} type="button"
              onClick={() => {
                setSource(s);
                onChange({});
                const next = capabilityForSource(capability, s);
                if (next !== capability) onCapabilityChange?.(next);
              }}
              className={cn('inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs font-medium',
                source === s ? 'border-blue-200 bg-blue-50 text-blue-600' : 'border-slate-200 text-slate-500 hover:bg-slate-50')}>
              <M.icon size={12} />{M.label}
            </button>
          );
        })}
      </div>

      {source === 'auto' && (
        <p className="text-[11px] text-slate-400">
          {capability === 'report' ? '不引用数据文件,仅按主题生成(可联网搜索)。' : '不指定数据集,由系统智能选表。'}
        </p>
      )}

      {source === 'dataset' && (
        <div className="space-y-1.5">
          {/* 按分类单选 / 多选 / 全选;多选走多表问数(multipleCubeIds) */}
          <DatasetMultiPicker registered={datasetAssets} cubes={cubes} loading={cubeLoading}
            onOpen={loadCubes} value={value} onChange={onChange} />
          {!value.cube_id && !(value.cube_ids || []).length && (
            <input className={inputCls} placeholder="或输入数据集名称(精确匹配时跳过智能选表)" value={value.cube_name || ''}
              onChange={(e) => onChange(e.target.value ? { cube_name: e.target.value } : {})} />
          )}
        </div>
      )}

      {source === 'dashboard' && (
        <div className="space-y-1.5">
          <select className={selectCls} value={value.asset_id || ''}
            onChange={(e) => onChange(e.target.value ? { asset_id: e.target.value } : {})}>
            <option value="">{dashboards.length ? '选择已登记的仪表板…' : '(暂无已登记仪表板,可直接粘贴地址)'}</option>
            {dashboards.map((a) => <option key={a.id} value={a.id}>{a.name}(图表 {a.chart_count})</option>)}
          </select>
          {capability === 'dashboard_qa' && value.asset_id && (
            <p className="text-[11px] leading-relaxed text-slate-400">
              按官方「小Q报表」做法:问题点到某张图 → 用它的数据集问;否则所选组件用到的数据集一起多表问数,由小Q选一张或多张表
              {value.qa_scope === 'dataset' && (
                <span className="ml-1 text-amber-600">
                  (这条是旧配置「挑一份数据集」,
                  <button type="button" className="underline" onClick={() => onChange({ ...value, qa_scope: undefined })}>改为新做法</button>)
                </span>
              )}
            </p>
          )}
          {/* 解读 / 报表问数:可确认组件范围(默认全部) */}
          {value.asset_id && (capability === 'insight' || capability === 'dashboard_qa') && (
            <ChartScopePicker assetId={value.asset_id} value={value.chart_ids}
              onChange={(ids) => onChange({ ...value, chart_ids: ids })} />
          )}
          {!value.asset_id && (
            <input className={inputCls} placeholder="或粘贴仪表板 / 数据门户地址、pageId" value={value.dashboard || ''}
              onChange={(e) => onChange(e.target.value ? { dashboard: e.target.value.trim() } : {})} />
          )}
        </div>
      )}

      {source === 'file' && (
        <div className="space-y-1.5">
          {capability === 'report' ? (
            <div className="max-h-36 space-y-1 overflow-auto rounded-lg border border-slate-200 p-2">
              {files.filter((f) => f.capabilities.includes('report')).map((f) => (
                <label key={f.id} className="flex items-center gap-2 text-xs text-slate-600">
                  <input type="checkbox" checked={reportFiles.includes(f.id)}
                    onChange={(e) => onChange({ ...value, file_ids: e.target.checked ? [...reportFiles, f.id] : reportFiles.filter((x) => x !== f.id), file_id: undefined })} />
                  {f.file_name}<span className="text-slate-300">{(f.size / 1024).toFixed(0)}KB</span>
                </label>
              ))}
              {files.length === 0 && <div className="text-xs text-slate-400">还没有上传文件</div>}
            </div>
          ) : (
            <select className={selectCls} value={value.file_id || ''} onChange={(e) => onChange(e.target.value ? { file_id: e.target.value } : {})}>
              <option value="">选择已上传文件…</option>
              {files.filter((f) => f.capabilities.includes(capability === 'insight' ? 'insight' : 'file_qa')).map((f) => (
                <option key={f.id} value={f.id}>{f.file_name}({(f.size / 1024).toFixed(0)}KB)</option>
              ))}
            </select>
          )}
          <input ref={fileInput} type="file" className="hidden"
            accept={capability === 'report' ? '.xls,.xlsx,.csv,.doc,.docx' : '.xls,.xlsx,.csv'}
            onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ''; }} />
          <button type="button" disabled={uploading || !connectionId} onClick={() => fileInput.current?.click()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-slate-300 px-3 py-1.5 text-xs text-slate-500 hover:border-blue-300 hover:text-blue-600 disabled:opacity-50">
            {uploading ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />}上传新文件(≤5MB)
          </button>
        </div>
      )}

      {source === 'data' && (
        <textarea className="min-h-[96px] w-full rounded-lg border border-slate-200 p-2 font-mono text-xs outline-none focus:border-blue-400"
          placeholder={'直接粘贴要解读的数据(Markdown 表格 / CSV 文本)\n| 月份 | 销售额 |\n| --- | --- |\n| 1月 | 100 |'}
          value={value.data || ''} onChange={(e) => onChange(e.target.value ? { data: e.target.value } : {})} />
      )}

      {capability === 'report' && (
        <label className="flex items-center gap-2 text-xs text-slate-500">
          <input type="checkbox" checked={value.web_search !== false}
            onChange={(e) => onChange({ ...value, web_search: e.target.checked })} />允许联网搜索补充行业信息
        </label>
      )}
    </div>
  );
};

export function describeTarget(t: Record<string, any> | undefined): string {
  if (!t) return '—';
  const parts: string[] = [];
  if (t.asset_name) parts.push(`仪表板:${t.asset_name}`);
  else if (t.asset_id) parts.push(`资产 ${String(t.asset_id).slice(0, 8)}`);
  if (t.page_id && !t.asset_id) parts.push(`pageId ${String(t.page_id).slice(0, 8)}`);
  if (t.dashboard) parts.push('仪表板地址');
  if (t.cube_id) parts.push(`数据集 ${String(t.cube_id).slice(0, 10)}`);
  if ((t.cube_ids || []).length > 1) parts.push(`多表问数 · 数据集 ${t.cube_ids.length} 个`);
  if (t.cube_name) parts.push(`数据集「${t.cube_name}」`);
  if (t.file_name) parts.push(`文件:${t.file_name}`);
  else if (t.file_id) parts.push('上传文件');
  if ((t.file_ids || []).length) parts.push(`引用文件 ${t.file_ids.length} 个`);
  if (t.data) parts.push('粘贴数据');
  if (t.qa_scope === 'dataset') parts.push('旧:挑一份数据集');
  if ((t.chart_ids || []).length) parts.push(`图表 ${t.chart_ids.length} 张`);
  return parts.join(' · ') || '自动';
}

/**
 * 景区选择器:**一个框**搞定搜索 + 下拉 + 展示。
 *
 * 为什么不是「搜索框 + 原生 select」两个框:两个框的话,用户得先在上面打字、
 * 再去下面挑,选完上面那行残留的关键字还会一直杵在那儿,看着像没选中。
 * 这里是一个可输入的下拉(combobox):
 *   未选中 —— 打字即搜(主数据的名称/别名/ID),下面浮出候选,点一条即选中;
 *   已选中 —— 框里显示「景区ID(景区名称)」,要改就点一下重新搜、重新选;
 *   右侧 × 解绑回集团级。
 *
 * 候选一律来自景区主数据 dim_prd_scenic_base_yr_f —— 不让手填 scenic_id:
 * 敲错一位的表现只是「那个景区看不到这条数据」,没有报错,也没人会往 ID 上想。
 */
export const ScenicSelect: React.FC<{
  value: string; onChange: (v: string) => void;
  /** 未选中时框里的灰字(筛选场景传「全部景区」,绑定场景传「集团级…」) */
  placeholder?: string;
  disabled?: boolean;
  /** 只列**已登记且已开通**的景区。
   *
   *  绑定场景一律传它:停用的景区绑上去是一条配了也不生效的记录,
   *  列在下拉里只会让人以为它能用 —— 现场就这么选到过一个已经停用的景区。 */
  registeredOnly?: boolean;
}> = ({ value, onChange, placeholder = '集团级(不绑定景区)', disabled, registeredOnly }) => {
  const [open, setOpen] = useState(false);
  const [keyword, setKeyword] = useState('');
  const [items, setItems] = useState<api.ScenicCatalogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  // 已选中景区的名称:值可能是从服务端带回来的(编辑已有记录),本地没有对应的候选行,
  // 所以按 ID 单独取一次名字 —— 否则框里只能显示一串编号
  const [pickedName, setPickedName] = useState('');
  const boxRef = useRef<HTMLDivElement | null>(null);
  // 往上还是往下弹:这个字段常常排在弹窗底部,固定往下弹会被弹窗的滚动区裁掉半截,
  // 看着像「点了没反应」。展开的瞬间量一次位置,下方不够高就往上弹。
  const [dropUp, setDropUp] = useState(false);

  // 取已选中景区的显示名(换了值就重取;主数据里查无此景区时留空,只显示 ID)
  useEffect(() => {
    let alive = true;
    if (!value) { setPickedName(''); return; }
    api.getScenicCatalogDetail(value)
      .then((r) => { if (alive) setPickedName(r.scenic_name || ''); })
      .catch(() => { if (alive) setPickedName(''); });
    return () => { alive = false; };
  }, [value]);

  // 搜索:防抖 250ms —— 每敲一个字就打一次主数据,列表会闪
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoading(true);
    const t = setTimeout(() => {
      api.getScenicCatalog({ keyword: keyword.trim() || undefined, page_size: 20,
                             registered: registeredOnly ? true : undefined,
                             enabled: registeredOnly ? true : undefined })
        .then((r) => { if (alive) { setItems(r.items); setTotal(r.total); } })
        .catch(() => { if (alive) { setItems([]); setTotal(0); } })
        .finally(() => { if (alive) setLoading(false); });
    }, 250);
    return () => { alive = false; clearTimeout(t); };
  }, [keyword, open, registeredOnly]);

  // 点框外收起:下拉是浮层,不收的话它会一直盖着下面的表单项
  useEffect(() => {
    if (!open) return;
    const onDocDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
        setKeyword('');
      }
    };
    document.addEventListener('mousedown', onDocDown);
    return () => document.removeEventListener('mousedown', onDocDown);
  }, [open]);

  // 绑定后的展示:景区ID(景区名称) —— ID 在前,因为排查问题时对的是 ID
  const display = value ? (pickedName ? `${value}（${pickedName}）` : value) : '';

  const toggleOpen = (next: boolean) => {
    if (next) {
      const rect = boxRef.current?.getBoundingClientRect();
      setDropUp(!!rect && window.innerHeight - rect.bottom < 240 && rect.top > 240);
    }
    setOpen(next);
  };

  const pick = (item: api.ScenicCatalogItem) => {
    onChange(item.scenic_id);
    setPickedName(item.scenic_name || '');
    setKeyword('');
    setOpen(false);
  };

  return (
    <div className="relative" ref={boxRef}>
      <input
        className={cn(inputCls, 'pr-14', value && !open && 'font-mono text-slate-700')}
        disabled={disabled}
        // 已选中且未展开时显示「ID(名称)」;一旦展开就交给关键字,让用户直接打字重搜
        value={open ? keyword : display}
        placeholder={open ? '输入景区名称 / 别名 / ID 搜索' : placeholder}
        onFocus={() => !disabled && toggleOpen(true)}
        onChange={(e) => { setKeyword(e.target.value); if (!open) toggleOpen(true); }}
        onKeyDown={(e) => { if (e.key === 'Escape') { setOpen(false); setKeyword(''); } }}
      />
      <div className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1">
        {value && !disabled && (
          <button type="button" title="解绑(回到集团级)" className="text-slate-300 hover:text-rose-500"
            onClick={() => { onChange(''); setPickedName(''); setKeyword(''); setOpen(false); }}>
            <X size={14} />
          </button>
        )}
        <button type="button" className="text-slate-300 hover:text-slate-500" disabled={disabled}
          onClick={() => { toggleOpen(!open); setKeyword(''); }}>
          <ChevronDown size={14} />
        </button>
      </div>

      {open && (
        <div className={cn('absolute z-30 max-h-56 w-full overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-lg',
                           dropUp ? 'bottom-full mb-1' : 'mt-1')}>
          {/* 「不绑定」永远排在第一条:解绑是常用操作,不该只能靠右侧那个小 × */}
          <button type="button" className="block w-full px-3 py-2 text-left text-xs text-slate-500 hover:bg-slate-50"
            onClick={() => { onChange(''); setPickedName(''); setKeyword(''); setOpen(false); }}>
            {placeholder}
          </button>
          {loading && <div className="px-3 py-3 text-center text-xs text-slate-300">搜索中…</div>}
          {!loading && items.length === 0 && (
            <div className="px-3 py-3 text-center text-xs text-slate-300">
              没有匹配的景区(候选来自景区主数据)
            </div>
          )}
          {/* 主数据上万条,下拉只取前 20 个 —— 不说清楚的话,搜不到的人会以为库里没有 */}
          {!loading && total > items.length && (
            <div className="border-b border-slate-100 px-3 py-1.5 text-[11px] text-slate-400">
              匹配 {total} 个,仅显示前 {items.length} 个,请输入更精确的关键字
            </div>
          )}
          {!loading && items.map((i) => (
            <button key={i.scenic_id} type="button"
              className={cn('block w-full px-3 py-2 text-left hover:bg-slate-50',
                            i.scenic_id === value && 'bg-blue-50/60')}
              onClick={() => pick(i)}>
              <div className="truncate text-sm text-slate-800">
                <span className="font-mono text-xs text-slate-500">{i.scenic_id}</span>
                <span className="ml-1">（{i.scenic_name}）</span>
              </div>
              <div className="truncate text-[11px] text-slate-400">
                {[i.province_name, i.city_name].filter(Boolean).join(' ')}
                {i.scenic_grade_level ? ` · ${i.scenic_grade_level}` : ''}
                {i.registered ? '' : ' · 未登记'}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
