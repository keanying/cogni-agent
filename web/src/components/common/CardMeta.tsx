import React from 'react';
import { Clock, Building2 } from 'lucide-react';
import { fmtUpdatedAt } from '../../lib/modalNavCore';

/**
 * 工具中心列表卡片底部的「最近修改时间 · 修改人 · 归属」。
 * 数据来自列表接口的 updated_at / updated_by_name / scenic_name / owner_name(后端 _list_meta)。
 */
export const CARD_GRID = 'grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-4';

export const CardMeta: React.FC<{ item: any }> = ({ item }) => (
  <div className="min-w-0 space-y-0.5 text-[11px] leading-4 text-slate-400">
    <div className="flex items-center gap-1 truncate" title={item.updated_at ? new Date(item.updated_at).toLocaleString() : ''}>
      <Clock size={11} className="shrink-0" />
      <span className="truncate">
        {fmtUpdatedAt(item.updated_at)}
        {item.updated_by_name ? <> · <span className="text-slate-500">{item.updated_by_name}</span> 修改</> : null}
      </span>
    </div>
    <div className="flex items-center gap-1 truncate">
      <Building2 size={11} className="shrink-0" />
      <span className="truncate">
        归属 <span className="text-slate-500">{item.scenic_name || '—'}</span>
        {item.owner_name ? <> · {item.owner_name}</> : null}
        {item.visibility ? <> · {item.visibility}</> : null}
      </span>
    </div>
  </div>
);
