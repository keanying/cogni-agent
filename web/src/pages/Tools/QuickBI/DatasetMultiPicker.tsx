import React, { useMemo, useState } from 'react';
import { ChevronDown, Search } from 'lucide-react';
import { cn } from '../../../lib/utils';
import type * as api from '../../../lib/api';
import { cubesToTarget, groupByCategory, selectedCubes, UNCATEGORIZED } from './datasetCategory';

/**
 * 编辑问题时选数据集:按分类分组,可单选 / 多选 / 按分类全选 / 全部全选。
 *
 * - 一个都不选 = 自动选表(不指定);
 * - 选 1 个 = cube_id;选多个 = cube_ids —— 后端走官方 multipleCubeIds 多表问数,
 *   小Q在所选数据集里选一张或多张表回答(以前多选只是「候选」,最后只挑一份去问)。
 * 候选 = 已登记的数据集(带分类)+ 当前用户可问数但没登记的(归到「未登记」)。
 */
export const DatasetMultiPicker: React.FC<{
  registered: api.QbiAsset[];
  cubes: { cube_id: string; cube_name: string }[] | null;
  loading?: boolean;
  onOpen?: () => void;
  value: Record<string, any>;
  onChange: (v: Record<string, any>) => void;
}> = ({ registered, cubes, loading, onOpen, value, onChange }) => {
  const [open, setOpen] = useState(false);
  const [kw, setKw] = useState('');
  const picked = selectedCubes(value);
  const pickedSet = new Set(picked);

  const options = useMemo(() => {
    const seen = new Set(registered.map((a) => a.ref_id));
    const reg = registered.filter((a) => a.active !== false)
      .map((a) => ({ id: a.ref_id, name: a.name, category: a.category || null }));
    const extra = (cubes || []).filter((c) => !seen.has(c.cube_id))
      .map((c) => ({ id: c.cube_id, name: c.cube_name, category: '未登记' }));
    return [...reg, ...extra];
  }, [registered, cubes]);
  const nameOf = (id: string) => options.find((o) => o.id === id)?.name || id;
  const k = kw.trim();
  const groups = groupByCategory(options.filter((o) => !k || o.name.includes(k) || (o.category || '').includes(k)));
  const allIds = options.map((o) => o.id);
  const allOn = allIds.length > 0 && allIds.every((id) => pickedSet.has(id));

  const set = (ids: string[]) => onChange(cubesToTarget(ids, value));
  const toggle = (id: string) => set(pickedSet.has(id) ? picked.filter((x) => x !== id) : [...picked, id]);
  const setMany = (ids: string[], on: boolean) =>
    set(on ? Array.from(new Set([...picked, ...ids])) : picked.filter((x) => !ids.includes(x)));

  const summary = picked.length === 0 ? '自动选表(不指定)'
    : picked.length === 1 ? nameOf(picked[0])
      : `已选 ${picked.length} 个数据集(多表问数):${picked.slice(0, 2).map(nameOf).join('、')}${picked.length > 2 ? '…' : ''}`;

  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <button type="button" onClick={() => { setOpen(!open); if (!open) onOpen?.(); }}
        className="flex h-10 w-full items-center justify-between px-3 text-left text-sm text-slate-700">
        <span className={cn('truncate', picked.length === 0 && 'text-slate-400')}>{summary}</span>
        <ChevronDown size={14} className={cn('shrink-0 text-slate-400 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="space-y-2 border-t border-slate-100 p-2">
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-400" />
              <input value={kw} onChange={(e) => setKw(e.target.value)} placeholder="搜索数据集 / 分类"
                className="h-8 w-full rounded-md border border-slate-200 pl-7 pr-2 text-xs outline-none focus:border-blue-400" />
            </div>
            <label className="flex shrink-0 items-center gap-1 text-xs text-slate-600">
              <input type="checkbox" checked={allOn} onChange={(e) => setMany(allIds, e.target.checked)} />全选
            </label>
            {picked.length > 0 && (
              <button type="button" className="shrink-0 text-[11px] text-slate-400 underline" onClick={() => set([])}>
                清空(自动选表)
              </button>
            )}
          </div>
          {loading && <div className="text-[11px] text-slate-400">正在拉取可问数数据集…</div>}
          <div className="max-h-64 space-y-2 overflow-y-auto pr-1">
            {groups.map((g) => {
              const ids = g.items.map((o) => o.id);
              const on = ids.every((id) => pickedSet.has(id));
              return (
                <div key={g.key}>
                  <label className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold text-slate-500">
                    <input type="checkbox" checked={on} onChange={(e) => setMany(ids, e.target.checked)} />
                    {g.label}<span className="font-normal text-slate-300">{g.items.length}</span>
                    {g.label === UNCATEGORIZED && <span className="font-normal text-slate-300">(在「数据资产」里给数据集加分类)</span>}
                  </label>
                  <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                    {g.items.map((o) => (
                      <label key={o.id} className={cn('flex cursor-pointer items-center gap-1.5 rounded-md border px-2 py-1 text-xs',
                        pickedSet.has(o.id) ? 'border-blue-200 bg-blue-50 text-slate-700' : 'border-slate-100 text-slate-500')}>
                        <input type="checkbox" checked={pickedSet.has(o.id)} onChange={() => toggle(o.id)} />
                        <span className="truncate" title={o.name}>{o.name}</span>
                      </label>
                    ))}
                  </div>
                </div>
              );
            })}
            {!groups.length && !loading && <div className="py-4 text-center text-xs text-slate-400">没有匹配的数据集</div>}
          </div>
          <p className="text-[11px] leading-relaxed text-slate-400">
            不选 = 自动选表;选 1 个 = 只问这个数据集;选多个 = 多表问数(官方 multipleCubeIds,小Q在所选数据集里选一张或多张表回答)。
          </p>
        </div>
      )}
    </div>
  );
};
