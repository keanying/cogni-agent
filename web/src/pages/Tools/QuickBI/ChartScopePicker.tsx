import React, { useEffect, useMemo, useState } from 'react';
import { BarChart3, Loader2, Search } from 'lucide-react';
import { Modal } from '../../../components/ui/PageHelpers';
import { Button } from '../../../components/ui/button';
import { cn } from '../../../lib/utils';
import * as api from '../../../lib/api';
import {
  chartLabel, dataCharts, fromChartIds, groupByCube, scopeSummary, toChartIds, type ScopeChart,
} from './chartScope';

/**
 * 解读 · 仪表板的「图表范围」:默认全选,可勾掉不想解读的图表。
 * 对齐 Quick BI 智能解读里的「请确认图表范围 · 全选」。
 *
 * 图表清单取自已登记资产的结构缓存(资产详情 meta.charts),不用每次去 Quick BI 拉。
 */
export const ChartScopePicker: React.FC<{
  assetId: string;
  value: string[] | undefined;
  onChange: (ids: string[] | undefined) => void;
  compact?: boolean;
}> = ({ assetId, value, onChange, compact }) => {
  const [charts, setCharts] = useState<ScopeChart[] | null>(null);
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [kw, setKw] = useState('');

  useEffect(() => {
    setCharts(null);
    if (!assetId) return;
    let alive = true;
    api.qbiGetAsset(assetId)
      .then((a) => { if (alive) setCharts(dataCharts(a.meta?.charts)); })
      .catch(() => { if (alive) setCharts([]); });
    return () => { alive = false; };
  }, [assetId]);

  const total = charts?.length ?? 0;
  const groups = useMemo(() => {
    const k = kw.trim();
    return groupByCube((charts || []).filter((c) => !k || chartLabel(c).includes(k) || (c.cube_name || '').includes(k)));
  }, [charts, kw]);
  const visible = groups.flatMap((g) => g.items);
  const allOn = !!charts?.length && charts.every((c) => picked.has(c.component_id));

  const openPicker = () => { setPicked(fromChartIds(value, charts || [])); setKw(''); setOpen(true); };
  const toggle = (id: string) => setPicked((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const setMany = (ids: string[], on: boolean) =>
    setPicked((p) => { const n = new Set(p); ids.forEach((id) => (on ? n.add(id) : n.delete(id))); return n; });

  if (charts === null) {
    return <span className="inline-flex items-center gap-1 text-[11px] text-slate-400"><Loader2 size={11} className="animate-spin" />读取图表…</span>;
  }
  if (!total) return null;

  return (
    <>
      <button type="button" onClick={openPicker} title="解读哪些图表(默认全选)"
        className={cn('inline-flex shrink-0 items-center gap-1 rounded-md border px-2 font-medium',
          compact ? 'h-7 text-[11px]' : 'h-8 text-xs',
          value?.length ? 'border-violet-200 bg-violet-50 text-violet-600' : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50')}>
        <BarChart3 size={12} />图表范围:{scopeSummary(value, total)}
      </button>
      {open && (
        <Modal title="请确认图表范围" wide onClose={() => setOpen(false)}
          footer={<>
            <span className="mr-auto self-center text-xs text-slate-500">已选择:图表 {picked.size} / {total}</span>
            <Button variant="outline" onClick={() => setOpen(false)}>取消</Button>
            <Button disabled={picked.size === 0} className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white"
              onClick={() => { onChange(toChartIds(picked, charts)); setOpen(false); }}>确定</Button>
          </>}>
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
              <input value={kw} onChange={(e) => setKw(e.target.value)} placeholder="搜索图表或数据集"
                className="h-8 w-full rounded-lg border border-slate-200 pl-8 pr-2 text-xs outline-none focus:border-blue-400" />
            </div>
            <label className="flex shrink-0 items-center gap-1.5 text-xs font-medium text-slate-600">
              <input type="checkbox" checked={allOn}
                onChange={(e) => setMany((charts || []).map((c) => c.component_id), e.target.checked)} />全选
            </label>
          </div>
          <p className="text-[11px] leading-relaxed text-slate-400">
            快照只覆盖看板当前页签;其它页签上的图表会按各自的数据集补取数据后一起解读,图表越多耗时越长。
          </p>
          <div className="space-y-3">
            {groups.map((g) => {
              const ids = g.items.map((c) => c.component_id);
              const on = ids.every((id) => picked.has(id));
              return (
                <div key={g.key}>
                  <label className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold text-slate-500">
                    <input type="checkbox" checked={on} onChange={(e) => setMany(ids, e.target.checked)} />
                    {g.label}<span className="font-normal text-slate-300">{g.items.length} 张</span>
                  </label>
                  <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                    {g.items.map((c) => (
                      <label key={c.component_id}
                        className={cn('flex cursor-pointer items-center gap-2 rounded-lg border px-2.5 py-1.5 text-xs',
                          picked.has(c.component_id) ? 'border-blue-200 bg-blue-50/60 text-slate-700' : 'border-slate-100 text-slate-500')}>
                        <input type="checkbox" checked={picked.has(c.component_id)} onChange={() => toggle(c.component_id)} />
                        <span className="truncate" title={chartLabel(c)}>{chartLabel(c)}</span>
                        <span className="ml-auto shrink-0 text-[10px] text-slate-300">{c.chart_type}</span>
                      </label>
                    ))}
                  </div>
                </div>
              );
            })}
            {!visible.length && <div className="py-6 text-center text-xs text-slate-400">没有匹配的图表</div>}
          </div>
        </Modal>
      )}
    </>
  );
};
