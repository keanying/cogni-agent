import React, { useEffect, useMemo, useState } from 'react';
import { Mountain, Search, Loader2, ChevronRight, Users, Clock } from 'lucide-react';
import { toast } from 'sonner';
import { Card, CardContent } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import * as api from '../../lib/api';
import { useNavigate } from 'react-router-dom';

/**
 * APP 运营 → 景区配置(25 号方案):列表页。
 *
 * 前置条件 = 被用户绑定过的景区(在用景区,与预热派单同口径)——主数据
 * (sys_scenics)万级,绝不全量浏览;列表行 = 主数据只读展示 × 配置状态,
 * 点击行进详情编辑。搜索/状态筛选只作用于在用景区子集内。
 */
const STATE_TABS = [
  { key: 'all', label: '全部' },
  { key: 'configured', label: '已配置' },
  { key: 'unconfigured', label: '未配置' },
] as const;

const ScenicConfig: React.FC = () => {
  const [items, setItems] = useState<api.ScenicConfigItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [kw, setKw] = useState('');
  const [state, setState] = useState<(typeof STATE_TABS)[number]['key']>('all');
  const navigate = useNavigate();

  const load = async () => {
    setLoading(true);
    try {
      const r = await api.listScenicConfigs(kw.trim() || undefined, state);
      setItems(r.items || []);
    } catch (e: any) { toast.error(e.message || '加载景区列表失败'); }
    finally { setLoading(false); }
  };

  useEffect(() => { load(); }, [state]);   // eslint-disable-line react-hooks/exhaustive-deps

  const filtered = useMemo(() => {
    const k = kw.trim().toLowerCase();
    if (!k) return items;
    return items.filter((d) =>
      d.scenic_id.toLowerCase().includes(k)
      || (d.scenic_name || '').toLowerCase().includes(k));
  }, [items, kw]);

  return (
    <div className="p-6">
      <div className="mb-4 flex items-center gap-2">
        <Mountain size={18} className="text-slate-500" />
        <h1 className="text-lg font-black text-slate-900">景区配置</h1>
        <span className="text-xs text-slate-400">
          景区级参数(承载量等)· 仅显示被用户绑定过的景区
        </span>
      </div>

      <Card className="border-slate-200 shadow-sm">
        <CardContent className="p-4">
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <div className="relative w-64">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
              <Input value={kw} onChange={(e) => setKw(e.target.value)}
                placeholder="搜索景区 ID / 名称…" className="h-9 rounded-xl pl-8" />
            </div>
            <div className="flex gap-1.5">
              {STATE_TABS.map((t) => (
                <button key={t.key} onClick={() => setState(t.key)}
                  className={cn('rounded-lg px-2.5 py-1 text-[11px] font-bold transition-all',
                    state === t.key ? 'bg-blue-600 text-white shadow-sm'
                      : 'bg-slate-100 text-slate-500 hover:bg-slate-200')}>
                  {t.label}
                </button>
              ))}
            </div>
            <span className="ml-auto text-[11px] text-slate-400">{filtered.length} 个在用景区</span>
          </div>

          {loading ? (
            <div className="flex justify-center py-16"><Loader2 className="animate-spin text-slate-300" size={28} /></div>
          ) : filtered.length === 0 ? (
            <div className="py-16 text-center text-sm text-slate-400">
              没有匹配的景区(列表只含被用户绑定过的景区)
            </div>
          ) : (
            <table className="w-full text-left text-[13px]">
              <thead>
                <tr className="border-b border-slate-100 text-[11px] uppercase tracking-wide text-slate-400">
                  <th className="px-3 py-2 font-bold">景区</th>
                  <th className="px-3 py-2 font-bold">景区 ID</th>
                  <th className="px-3 py-2 font-bold">等级</th>
                  <th className="px-3 py-2 font-bold">绑定用户</th>
                  <th className="px-3 py-2 font-bold">日承载量</th>
                  <th className="px-3 py-2 font-bold">更新时间</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {filtered.map((d) => (
                  <tr key={d.scenic_id} onClick={() => navigate(`/app-ops/scenic-config/${d.scenic_id}`)}
                    className="cursor-pointer border-b border-slate-50 transition-colors hover:bg-slate-50">
                    <td className="px-3 py-2.5">
                      <div className="font-bold text-slate-800">{d.scenic_name || '(主数据缺失)'}</div>
                      <div className="mt-0.5 flex items-center gap-1.5">
                        <Badge className={cn('border-none text-[10px] font-bold',
                          d.configured ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500')}>
                          {d.configured ? '已配置' : '未配置'}
                        </Badge>
                        {!d.is_enabled && (
                          <Badge className="border-none bg-red-50 text-[10px] font-bold text-red-500">主数据停用</Badge>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2.5 font-mono text-[11px] text-slate-500">{d.scenic_id}</td>
                    <td className="px-3 py-2.5 text-slate-600">{d.scenic_grade_level || '-'}</td>
                    <td className="px-3 py-2.5">
                      <span className="flex items-center gap-1 text-slate-600"><Users size={12} className="text-slate-400" />{d.bound_users}</span>
                    </td>
                    <td className="px-3 py-2.5 font-bold text-slate-800">
                      {d.daily_capacity ? `${d.daily_capacity.toLocaleString()} 人次/天` : '-'}
                    </td>
                    <td className="px-3 py-2.5">
                      {d.updated_at ? (
                        <span className="flex items-center gap-1 text-[11px] text-slate-400">
                          <Clock size={11} />{new Date(d.updated_at).toLocaleString('zh-CN', { hour12: false })}
                        </span>
                      ) : '-'}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <ChevronRight size={14} className="text-slate-300" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
};

export default ScenicConfig;
