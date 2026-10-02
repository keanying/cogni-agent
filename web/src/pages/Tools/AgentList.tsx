
import React, { useEffect, useState } from 'react';
import { Plus, Search, Loader2, Trash2, Settings2 } from 'lucide-react';
import { CopyId } from '../../components/ui/PageHelpers';
import { Card, CardContent } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { Avatar, AvatarFallback, AvatarImage } from '../../components/ui/avatar';
import { cn } from '../../lib/utils';
import { useLocation, useNavigate } from 'react-router-dom';
import { openInModal } from '../../lib/modalNav';
import { CARD_GRID, CardMeta } from '../../components/common/CardMeta';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { ConfirmDialog } from '../../components/ui/ConfirmDialog';

const AgentList: React.FC = () => {
  const [agents, setAgents] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<any>(null);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  const load = async () => {
    setLoading(true);
    try { setAgents((await api.listAgents()).items); }
    catch (e: any) { toast.error(e.message || '加载 Agent 列表失败'); }
    finally { setLoading(false); }
  };
  // 查看/修改在弹窗里做;弹窗关闭(跳回列表)时 location.key 变化 → 重新拉一次
  useEffect(() => { load(); }, [location.key]);

  const toggleStatus = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try { await api.toggleAgent(id); toast.success('状态已更新'); load(); }
    catch (err: any) { toast.error(err.message || '操作失败'); }
  };

  const confirmDelete = async (force?: boolean) => {
    if (!deleteTarget) return;
    setDeleting(true);
    try { await api.deleteAgent(deleteTarget.id, !!force); toast.success(force ? '已强制删除并归档' : '已删除'); setDeleteTarget(null); load(); }
    catch (err: any) { toast.error(err.message || '删除失败'); }
    finally { setDeleting(false); }
  };

  const filtered = agents.filter((a) => !keyword || a.name.includes(keyword));

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">Agent 中心</h1>
          <p className="text-slate-500 text-sm mt-1">赋予 AI 独特的人格设定与能力集，创建可交付的数字员工。</p>
        </div>
        <Button onClick={() => navigate('/tools/agent/new')} className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl gap-2 shadow-lg shadow-blue-200 h-11 px-6">
          <Plus size={18} />
          新建 Agent
        </Button>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/50 backdrop-blur-sm p-4">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
          <Input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="搜索 Agent 名称..." className="pl-10 h-11 bg-white border-slate-200 rounded-xl focus-visible:ring-[#2563eb]" />
        </div>
      </Card>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-20 text-slate-300 text-sm">暂无 Agent，点击右上角新建</div>
      ) : (
        <div className={CARD_GRID}>
          {filtered.map((agent) => (
            <Card key={agent.id} onClick={() => openInModal(navigate, location, `/tools/agent/detail?id=${agent.id}`)}
              className={cn('group border-slate-200 shadow-sm hover:shadow-md hover:border-blue-200 transition-all bg-white overflow-hidden flex flex-col cursor-pointer', !agent.active && 'opacity-75')}>
              <div className="p-4 flex-1">
                <div className="flex items-start gap-2.5">
                  <Avatar className="h-9 w-9 shrink-0 border border-white shadow-sm rounded-xl">
                    {agent.avatar_url && <AvatarImage src={agent.avatar_url} />}
                    <AvatarFallback className="bg-blue-600 text-white text-sm font-bold rounded-xl">{agent.name[0]}</AvatarFallback>
                  </Avatar>
                  <div className="min-w-0 flex-1">
                    <h3 className="text-sm font-bold text-slate-900 group-hover:text-blue-600 transition-colors truncate" title={agent.name}>{agent.name}</h3>
                    <CopyId id={agent.id} />
                  </div>
                  <button onClick={(e) => toggleStatus(agent.id, e)} title="点击切换在线 / 离线"
                    className={cn('shrink-0 h-6 px-1.5 rounded-md text-[10px] font-bold transition-colors',
                      agent.active ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-100 text-slate-400')}>
                    {agent.active ? '在线' : '离线'}
                  </button>
                </div>
                <p className="mt-2 text-xs text-slate-500 line-clamp-2" title={agent.persona_desc}>
                  {agent.persona_desc || <span className="italic text-slate-300">未设置人格</span>}
                </p>
                <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                  {/* 15 号文档:无默认版本概念,发布态按后端 published_count(已发布版本数)推导,
                      不再信主表 status/default_version;已发布可附 assigned_count(已指派用户数) */}
                  <Badge variant="outline" className={cn('rounded-md font-medium text-[10px] px-1.5 py-0', (agent.published_count ?? 0) > 0
                    ? 'text-emerald-600 border-emerald-100 bg-emerald-50'
                    : 'text-amber-600 border-amber-100 bg-amber-50')}>
                    {(agent.published_count ?? 0) > 0 ? `已发布 ${agent.published_count} 版` : '未发布'}
                  </Badge>
                  <Badge variant="outline" className="rounded-md border-slate-100 bg-slate-50 text-slate-500 font-medium text-[10px] px-1.5 py-0">
                    {(agent.bound_skill_ids || []).length} 个技能
                  </Badge>
                  <Badge variant="outline" className="rounded-md border-slate-100 bg-slate-50 text-slate-500 font-medium text-[10px] px-1.5 py-0"
                    title="QPS 上限 / 日调用上限">
                    {agent.rate_limit_qps} QPS · 日 {agent.rate_limit_daily}
                  </Badge>
                  {(agent.assigned_count ?? 0) > 0 && (
                    <Badge variant="outline" className="rounded-md border-blue-100 bg-blue-50 text-blue-600 font-medium text-[10px] px-1.5 py-0">
                      已指派 {agent.assigned_count} 人
                    </Badge>
                  )}
                </div>
              </div>
              <div className="px-4 py-2 bg-slate-50/50 border-t flex items-center justify-between gap-2">
                <CardMeta item={agent} />
                <div className="flex shrink-0 gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg" title="修改" onClick={(e) => { e.stopPropagation(); openInModal(navigate, location, `/tools/agent/edit?id=${agent.id}`); }}>
                    <Settings2 size={14} className="text-slate-500" />
                  </Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg text-rose-500 hover:text-rose-600 hover:bg-rose-50" title="删除" onClick={(e) => { e.stopPropagation(); setDeleteTarget(agent); }}>
                    <Trash2 size={14} />
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        title="删除 Agent"
        description={<>确定删除智能体「<span className="font-bold text-slate-800">{deleteTarget?.name}</span>」？<br />删除后将归档，历史版本记录保留，不可恢复。</>}
        loading={deleting}
        forceEnabled
        onConfirm={confirmDelete}
        onClose={() => { if (!deleting) setDeleteTarget(null); }}
      />
    </div>
  );
};

export default AgentList;
