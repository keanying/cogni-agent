
import React, { useEffect, useState } from 'react';
import { Plus, Search, RefreshCw, Box, Trash2, Settings2, Loader2, Sparkles } from 'lucide-react';
import { Card, CardContent } from '../../components/ui/card';
import { CopyId } from '../../components/ui/PageHelpers';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { useLocation, useNavigate } from 'react-router-dom';
import { openInModal } from '../../lib/modalNav';
import { CARD_GRID, CardMeta } from '../../components/common/CardMeta';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const STATUS_META: Record<string, { color: string; label: string }> = {
  connected: { color: 'text-emerald-500', label: '已连接' },
  pending: { color: 'text-slate-400', label: '待检测' },
  auth_failed: { color: 'text-rose-500', label: '认证失败' },
  timeout: { color: 'text-amber-500', label: '超时' },
};

const MCPList: React.FC = () => {
  const [servers, setServers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const navigate = useNavigate();
  const location = useLocation();

  const load = async () => {
    setLoading(true);
    try { setServers((await api.listMcpServers()).items); }
    catch (e: any) { toast.error(e.message || '加载 MCP Server 列表失败'); }
    finally { setLoading(false); }
  };
  // 查看/修改在弹窗里做;弹窗关闭(跳回列表)时 location.key 变化 → 重新拉一次
  useEffect(() => { load(); }, [location.key]);

  const handleDiscover = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try { const r = await api.discoverMcpTools(id); toast.success(`发现并导入 ${r.imported_count} 个工具`); load(); }
    catch (err: any) { toast.error(err.message || '发现失败'); }
  };

  const filtered = servers.filter((s) => !keyword || s.name.includes(keyword) || s.url.includes(keyword));

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">MCP Server 管理</h1>
          <p className="text-slate-500 text-sm mt-1">外部工具发现协议，自动同步和接入第三方工具能力。</p>
        </div>
        <Button onClick={() => navigate('/tools/mcp/new')} className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl gap-2 shadow-lg shadow-blue-200 h-11 px-6">
          <Plus size={18} />
          接入 MCP Server
        </Button>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/50 backdrop-blur-sm">
        <CardContent className="p-4">
          <div className="flex flex-col md:flex-row gap-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
              <Input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="搜索 Server 名称或 URL..." className="pl-10 h-11 bg-white border-slate-200 rounded-xl focus-visible:ring-[#2563eb]" />
            </div>
            <Button variant="outline" className="h-11 rounded-xl border-slate-200 bg-white gap-2 text-slate-600 font-medium" onClick={load}>
              <RefreshCw size={16} />
              刷新
            </Button>
          </div>
        </CardContent>
      </Card>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-20 text-slate-300 text-sm">暂无 MCP Server</div>
      ) : (
        <div className={CARD_GRID}>
          {filtered.map((server) => {
            const meta = STATUS_META[server.connect_status] || { color: 'text-slate-400', label: server.connect_status };
            return (
              <Card key={server.id} onClick={() => openInModal(navigate, location, `/tools/mcp/detail?id=${server.id}`)}
                className="group border-slate-200 shadow-sm hover:shadow-md hover:border-blue-200 transition-all bg-white overflow-hidden flex flex-col cursor-pointer">
                <div className="p-4 flex-1">
                  <div className="flex items-start gap-2.5">
                    <div className="h-8 w-8 shrink-0 rounded-lg bg-blue-50 flex items-center justify-center text-blue-600">
                      <Box size={15} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <h3 className="text-sm font-bold text-slate-900 group-hover:text-blue-600 transition-colors truncate" title={server.name}>{server.name}</h3>
                      <CopyId id={server.id} />
                    </div>
                    <div className="flex shrink-0 items-center gap-1 mt-1">
                      <div className={cn('h-1.5 w-1.5 rounded-full', meta.color.replace('text-', 'bg-'))} />
                      <span className={cn('text-[10px] font-bold', meta.color)}>{meta.label}</span>
                    </div>
                  </div>
                  <p className="mt-2 text-xs text-slate-500 line-clamp-1 font-mono" title={server.url}>{server.url}</p>
                  <div className="mt-2.5">
                    <Button size="sm" variant="outline" className="h-6 px-2 text-[11px] gap-1" onClick={(e) => handleDiscover(server.id, e)}>
                      <Sparkles size={12} />发现工具
                    </Button>
                  </div>
                </div>
                <div className="px-4 py-2 bg-slate-50/50 border-t flex items-center justify-between gap-2">
                  <CardMeta item={server} />
                  <div className="flex shrink-0 gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg" title="修改" onClick={(e) => { e.stopPropagation(); openInModal(navigate, location, `/tools/mcp/edit?id=${server.id}`); }}>
                      <Settings2 size={14} className="text-slate-500" />
                    </Button>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default MCPList;
