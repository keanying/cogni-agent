
import React, { useEffect, useState } from 'react';
import { Plus, Search, Database, Table as TableIcon, Settings2, Loader2, Power } from 'lucide-react';
import { Card, CardContent } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { confirmDialog } from '../../components/ui/confirm';

const TYPE_COLOR: Record<string, string> = {
  mysql: 'bg-blue-50 text-blue-600 border-blue-100',
  starrocks: 'bg-purple-50 text-purple-600 border-purple-100',
  clickhouse: 'bg-amber-50 text-amber-600 border-amber-100',
  selectdb: 'bg-emerald-50 text-emerald-600 border-emerald-100',
  postgres: 'bg-indigo-50 text-indigo-600 border-indigo-100',
};

const DatabaseKnowledge: React.FC = () => {
  const [dataSources, setDataSources] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const navigate = useNavigate();

  const load = async () => {
    setLoading(true);
    try { setDataSources((await api.listDatasources()).items); }
    catch (e: any) { toast.error(e.message || '加载数据源列表失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const handleDelete = async (id: string) => {
    if (!await confirmDialog({ title: '停用该数据源',
      message: '停用后绑定它的工具执行会直接失败,配置与表结构仍保留,可随时启用。',
      confirmLabel: '停用', danger: true })) return;
    try { await api.deleteDatasource(id); toast.success('已停用'); load(); } catch (e: any) { toast.error(e.message); }
  };

  const filtered = dataSources.filter((d) => !keyword || d.name.includes(keyword));

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">数据库知识</h1>
          <p className="text-slate-500 text-sm mt-1">连接结构化数据源，为工具提供精准的数据存取与分析能力。</p>
        </div>
        <Button onClick={() => navigate('/knowledge/database/new')} className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl gap-2 shadow-lg shadow-blue-200 h-11 px-6 font-bold">
          <Plus size={18} />新增数据源
        </Button>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/50 backdrop-blur-sm p-4">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
          <Input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="搜索数据源名称..." className="pl-10 h-11 bg-white border-slate-200 rounded-xl focus-visible:ring-[#2563eb]" />
        </div>
      </Card>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>
      ) : (
        <div className="border rounded-2xl bg-white shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead><tr className="text-left border-b bg-slate-50/80">
              <th className="p-4 font-black text-slate-500 uppercase text-[10px] tracking-widest">名称</th>
              <th className="p-4 font-black text-slate-500 uppercase text-[10px] tracking-widest">数据库类型</th>
              <th className="p-4 font-black text-slate-500 uppercase text-[10px] tracking-widest text-center">归属</th>
              <th className="p-4 font-black text-slate-500 uppercase text-[10px] tracking-widest text-center">状态</th>
              <th className="p-4 font-black text-slate-500 uppercase text-[10px] tracking-widest text-right">操作</th>
            </tr></thead>
            <tbody className="divide-y">
              {filtered.map((ds) => (
                <tr key={ds.id} className="group hover:bg-slate-50/50 transition-colors">
                  <td className="p-4"><div className="flex items-center gap-3">
                    <div className="h-9 w-9 rounded-lg bg-blue-50 flex items-center justify-center text-blue-600 border border-blue-100"><Database size={18} /></div>
                    <span className="font-bold text-slate-800">{ds.name}</span>
                  </div></td>
                  <td className="p-4"><Badge variant="outline" className={cn('rounded-md font-black text-[10px] uppercase tracking-wider', TYPE_COLOR[ds.db_type] || 'bg-slate-50 text-slate-600')}>{ds.db_type}</Badge></td>
                  <td className="p-4 text-center"><Badge variant="outline" className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">{ds.scope === 'group' ? '集团级' : ds.scenic_id}</Badge></td>
                  <td className="p-4"><div className="flex items-center justify-center gap-1.5">
                    <div className={cn('h-1.5 w-1.5 rounded-full', ds.active ? 'bg-emerald-500' : 'bg-slate-300')} />
                    <span className={cn('text-xs font-bold uppercase tracking-tighter', ds.active ? 'text-emerald-600' : 'text-slate-400')}>{ds.active ? 'Active（启用）' : 'Disabled（停用）'}</span>
                  </div></td>
                  <td className="p-4 text-right">
                    <div className="flex justify-end gap-1 opacity-0 group-hover:opacity-100 transition-all">
                      <Button variant="ghost" size="sm" className="h-8 rounded-lg text-xs font-bold gap-1 text-slate-600" onClick={() => navigate(`/knowledge/database/detail?id=${ds.id}`)}>
                        <TableIcon size={14} /> 管理表
                      </Button>
                      <Button variant="ghost" size="icon" className="h-8 w-8 rounded-lg text-rose-400" onClick={() => handleDelete(ds.id)}><Power size={16} /></Button>
                    </div>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && <tr><td colSpan={5} className="p-10 text-center text-slate-300">暂无数据源</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
};

export default DatabaseKnowledge;
