
import React, { useEffect, useState } from 'react';
import {
  ArrowLeft, Search, Table as TableIcon, Code, Eye, ChevronDown, ChevronUp, Variable, Loader2, CheckCircle2,
} from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../../components/ui/tabs';
import { Input } from '../../components/ui/input';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const DatabaseDetail: React.FC = () => {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const id = params.get('id');
  const [datasources, setDatasources] = useState<any[]>([]);
  const [tables, setTables] = useState<{ table_name: string; registered: boolean }[]>([]);
  const [ddlCache, setDdlCache] = useState<Record<string, { ddl: string; preview: any }>>({});
  const [expandedTable, setExpandedTable] = useState<string | null>(null);
  const [tableSearch, setTableSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [customSources, setCustomSources] = useState<any[]>([]);
  const [virtualName, setVirtualName] = useState('');
  const [virtualSql, setVirtualSql] = useState('SELECT * FROM your_table WHERE 1=1');

  const ds = datasources.find((d) => d.id === id);

  const load = async () => {
    setLoading(true);
    try {
      const [dsList, cs] = await Promise.all([api.listDatasources(), api.listCustomSources()]);
      setDatasources(dsList.items);
      setCustomSources(cs.items);
      if (id) setTables((await api.listDatasourceTables(id)).items);
    } catch (e: any) {
      toast.error(e.message || '加载失败（请检查数据源连接配置）');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, [id]);

  const toggleTable = async (tableName: string) => {
    if (expandedTable === tableName) { setExpandedTable(null); return; }
    setExpandedTable(tableName);
    if (!ddlCache[tableName] && id) {
      try {
        const result = await api.getTableDdl(id, tableName);
        setDdlCache((prev) => ({ ...prev, [tableName]: result }));
      } catch (e: any) { toast.error(e.message || '读取 DDL 失败'); }
    }
  };

  const handleRegister = async (tableName: string) => {
    if (!id) return;
    try { await api.registerTable(id, { table_name: tableName, description: '', columns_doc: {} }); toast.success(`表 ${tableName} 已登记`); load(); }
    catch (e: any) { toast.error(e.message || '登记失败'); }
  };

  const handleCreateVirtual = async () => {
    if (!virtualName.trim() || !id) { toast.error('请填写虚拟表名称'); return; }
    if (!virtualSql.trim().toLowerCase().startsWith('select')) { toast.error('虚拟数据源的视图逻辑只允许 SELECT'); return; }
    try { await api.createCustomSource({ name: virtualName, base_datasource_id: id, build_sql: virtualSql }); toast.success('虚拟数据源已创建'); load(); }
    catch (e: any) { toast.error(e.message || '创建失败'); }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  if (!ds) return <div className="text-center py-20 text-slate-400">未找到该数据源</div>;

  const filteredTables = tables.filter((t) => !tableSearch || t.table_name.includes(tableSearch));

  return (
    <div className="max-w-7xl mx-auto space-y-6 animate-in fade-in duration-500">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => navigate('/knowledge/database')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-black text-slate-900 tracking-tight">{ds.name}</h1>
              <Badge className="bg-blue-50 text-blue-600 border-blue-100 font-black tracking-widest text-[10px] uppercase">{ds.db_type}</Badge>
            </div>
          </div>
        </div>
      </div>

      <Tabs defaultValue="tables" className="w-full">
        <TabsList className="bg-slate-100/50 p-1 border rounded-xl gap-1">
          <TabsTrigger value="tables" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-6 py-2 text-xs uppercase tracking-widest">表登记与管理</TabsTrigger>
          <TabsTrigger value="virtual" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-6 py-2 text-xs uppercase tracking-widest">自定义虚拟数据源</TabsTrigger>
        </TabsList>

        <div className="mt-6">
          <TabsContent value="tables" className="space-y-6">
            <Card className="border-slate-200 shadow-sm overflow-hidden">
              <div className="p-4 border-b bg-slate-50/50 flex items-center justify-between">
                <div className="relative flex-1 max-w-sm">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
                  <Input value={tableSearch} onChange={(e) => setTableSearch(e.target.value)} placeholder="搜索表名..." className="pl-9 h-9 text-xs rounded-lg" />
                </div>
                <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">共发现 {tables.length} 张表（实时连接查询）</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead><tr className="text-left border-b bg-slate-50">
                    <th className="p-4 font-black text-slate-500 uppercase text-[10px] tracking-widest">表名</th>
                    <th className="p-4 font-black text-slate-500 uppercase text-[10px] tracking-widest text-center">登记状态</th>
                    <th className="p-4 font-black text-slate-500 uppercase text-[10px] tracking-widest text-right">操作</th>
                  </tr></thead>
                  <tbody className="divide-y">
                    {filteredTables.map((table) => (
                      <React.Fragment key={table.table_name}>
                        <tr className="group hover:bg-slate-50/50 cursor-pointer" onClick={() => toggleTable(table.table_name)}>
                          <td className="p-4"><div className="flex items-center gap-3">
                            <div className="h-8 w-8 rounded-lg bg-slate-100 flex items-center justify-center text-slate-400 group-hover:text-blue-500 transition-colors"><TableIcon size={16} /></div>
                            <span className="font-bold text-slate-800 font-mono text-xs">{table.table_name}</span>
                          </div></td>
                          <td className="p-4 text-center">{table.registered ? <Badge className="bg-emerald-50 text-emerald-600 border-none text-[10px]">已登记</Badge> : <Badge variant="outline" className="text-[10px] text-slate-400">未登记</Badge>}</td>
                          <td className="p-4 text-right flex items-center justify-end gap-2">
                            {!table.registered && (
                              <Button variant="outline" size="sm" className="h-7 text-[10px]" onClick={(e) => { e.stopPropagation(); handleRegister(table.table_name); }}>登记</Button>
                            )}
                            {expandedTable === table.table_name ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
                          </td>
                        </tr>
                        {expandedTable === table.table_name && (
                          <tr><td colSpan={3} className="p-0 border-b">
                            <div className="bg-slate-50 p-6 space-y-6">
                              {!ddlCache[table.table_name] ? (
                                <div className="flex justify-center py-6"><Loader2 className="animate-spin text-slate-300" size={20} /></div>
                              ) : (
                                <>
                                  <div className="space-y-3">
                                    <h5 className="text-[10px] font-black text-slate-400 uppercase tracking-widest flex items-center gap-2"><Code size={12} /> 建表 DDL</h5>
                                    <div className="bg-slate-900 rounded-xl p-4 overflow-x-auto">
                                      <pre className="text-blue-400 font-mono text-xs leading-relaxed">{ddlCache[table.table_name].ddl}</pre>
                                    </div>
                                  </div>
                                  <div className="space-y-3">
                                    <h5 className="text-[10px] font-black text-slate-400 uppercase tracking-widest flex items-center gap-2"><Eye size={12} /> 数据预览（前 100 行）</h5>
                                    <div className="border rounded-xl bg-white overflow-hidden overflow-x-auto">
                                      <table className="w-full text-[10px] text-slate-600 font-mono">
                                        <thead className="bg-slate-50 border-b uppercase"><tr>{ddlCache[table.table_name].preview.columns.map((c: string) => <th key={c} className="p-2 border-r">{c}</th>)}</tr></thead>
                                        <tbody className="divide-y">
                                          {ddlCache[table.table_name].preview.rows.slice(0, 10).map((row: any, i: number) => (
                                            <tr key={i}>{ddlCache[table.table_name].preview.columns.map((c: string) => <td key={c} className="p-2 border-r">{String(row[c])}</td>)}</tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </div>
                                  </div>
                                </>
                              )}
                            </div>
                          </td></tr>
                        )}
                      </React.Fragment>
                    ))}
                    {filteredTables.length === 0 && <tr><td colSpan={3} className="p-10 text-center text-slate-300">暂无表</td></tr>}
                  </tbody>
                </table>
              </div>
            </Card>
          </TabsContent>

          <TabsContent value="virtual" className="space-y-6">
            <Card className="border-slate-200 shadow-sm overflow-hidden">
              <CardHeader className="border-b bg-slate-50/50 py-4 px-6"><CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600"><Variable size={16} className="text-emerald-600" /> 构建虚拟数据源 (SQL View)</CardTitle></CardHeader>
              <CardContent className="p-6 space-y-6">
                <p className="text-sm text-slate-500">基于已登记的源表，用自定义 SQL 视图逻辑构建一个"虚拟数据源"，同样纳入表登记体系供上层调用（仅允许 SELECT）。</p>
                <div className="space-y-2"><label className="text-xs font-bold text-slate-700 uppercase">虚拟表名称</label><Input value={virtualName} onChange={(e) => setVirtualName(e.target.value)} placeholder="如：v_daily_sales_summary" className="h-11 rounded-xl" /></div>
                <div className="bg-slate-900 rounded-xl p-4">
                  <div className="flex items-center justify-between mb-2"><span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">SQL EDITOR</span></div>
                  <textarea value={virtualSql} onChange={(e) => setVirtualSql(e.target.value)} className="w-full h-48 bg-transparent text-emerald-400 font-mono text-sm outline-none resize-none" />
                </div>
                <Button className="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-black rounded-xl h-11" onClick={handleCreateVirtual}>保存虚拟数据源</Button>
                {customSources.filter((c) => c.base_datasource_id === id).length > 0 && (
                  <div className="space-y-2 pt-2">
                    <div className="text-[10px] font-bold text-slate-400 uppercase">已创建的虚拟数据源</div>
                    {customSources.filter((c) => c.base_datasource_id === id).map((c) => (
                      <div key={c.id} className="flex items-center gap-2 p-2 rounded-lg bg-slate-50 text-xs"><CheckCircle2 size={13} className="text-emerald-500" />{c.name}</div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </div>
      </Tabs>
    </div>
  );
};

export default DatabaseDetail;
