
import React, { useEffect, useState } from 'react';
import { ArrowLeft, Settings2, Sparkles, Activity, Clock, Loader2 } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { useSearchParams } from 'react-router-dom';
import { useModalNavigate } from '../../lib/modalNav';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { cn } from '../../lib/utils';

const STATUS_META: Record<string, { color: string; label: string }> = {
  connected: { color: 'text-emerald-600 bg-emerald-100', label: 'Connected' },
  pending: { color: 'text-slate-500 bg-slate-100', label: 'Pending' },
  auth_failed: { color: 'text-rose-600 bg-rose-100', label: 'Auth Failed' },
  timeout: { color: 'text-amber-600 bg-amber-100', label: 'Timeout' },
};

const MCPDetail: React.FC = () => {
  const navigate = useModalNavigate(); // 在弹窗里打开时,跳详情/编辑留在弹窗、跳回列表即关闭
  const [params] = useSearchParams();
  const id = params.get('id');
  const [server, setServer] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [discoverResult, setDiscoverResult] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try { const r = await api.listMcpServers(); setServer(r.items.find((s: any) => s.id === id) || null); }
    catch (e: any) { toast.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, [id]);

  const handleDiscover = async () => {
    try { const r = await api.discoverMcpTools(id!); setDiscoverResult(`发现并导入 ${r.imported_count} 个工具`); toast.success('工具发现完成'); }
    catch (e: any) { toast.error(e.message || '发现失败'); }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  if (!server) return <div className="text-center py-20 text-slate-400">未找到该 Server</div>;
  const meta = STATUS_META[server.connect_status] || { color: 'text-slate-500 bg-slate-100', label: server.connect_status };

  return (
    <div className="max-w-4xl mx-auto space-y-6 animate-in fade-in duration-500">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => navigate('/tools/mcp')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-bold text-slate-900 tracking-tight">{server.name}</h1>
              <Badge className={cn('border-none', meta.color)}>{meta.label}</Badge>
            </div>
            <p className="text-slate-500 text-sm font-mono">{server.url}</p>
          </div>
        </div>
        <div className="flex gap-3">
          <Button variant="outline" className="rounded-xl h-10 gap-2 font-medium" onClick={() => navigate(`/tools/mcp/edit?id=${id}`)}>
            <Settings2 size={16} /> 编辑配置
          </Button>
          <Button className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl h-10 px-6 font-bold shadow-lg shadow-blue-100 gap-2" onClick={handleDiscover}>
            <Sparkles size={16} /> 发现工具
          </Button>
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardHeader className="border-b bg-slate-50/50 py-4"><CardTitle className="text-sm font-bold">Server 状态</CardTitle></CardHeader>
        <CardContent className="p-5 space-y-6">
          <div className="flex flex-col items-center py-4 bg-slate-50 rounded-2xl border border-slate-100 gap-3">
            <div className={cn('h-16 w-16 rounded-full flex items-center justify-center shadow-inner', meta.color)}>
              <Activity size={32} />
            </div>
            <div className="text-center">
              <div className="text-xl font-black text-slate-900 tracking-tight">{meta.label}</div>
            </div>
          </div>
          <div className="space-y-4">
            <div className="flex items-center justify-between"><span className="text-xs text-slate-500 font-medium">认证方式</span><span className="text-xs font-bold text-slate-700">{server.auth_type}</span></div>
            <div className="flex items-center justify-between"><span className="text-xs text-slate-500 font-medium">可见范围</span><span className="text-xs font-bold text-slate-700 uppercase">{server.visibility}</span></div>
            <div className="h-px bg-slate-100" />
            <div className="flex items-center gap-3">
              <Clock size={14} className="text-slate-400" />
              <div className="text-[10px] text-slate-500">接入时间: <span className="font-bold text-slate-700">{new Date(server.created_at).toLocaleString()}</span></div>
            </div>
          </div>
          {discoverResult && <div className="p-3 rounded-xl bg-emerald-50 border border-emerald-100 text-sm text-emerald-700">{discoverResult}，可在「工具中心 · Tool」列表查看导入结果。</div>}
        </CardContent>
      </Card>
    </div>
  );
};

export default MCPDetail;
