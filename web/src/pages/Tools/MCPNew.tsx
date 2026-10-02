
import React, { useEffect, useState } from 'react';
import { ArrowLeft, Link as LinkIcon, ShieldCheck, Loader2, Sparkles, Rocket } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { useSearchParams } from 'react-router-dom';
import { useModalNavigate } from '../../lib/modalNav';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import ParamRefPicker from '../../components/common/ParamRefPicker';

const MCPNew: React.FC = () => {
  const navigate = useModalNavigate(); // 在弹窗里打开时,跳详情/编辑留在弹窗、跳回列表即关闭
  const [params] = useSearchParams();
  const editId = params.get('id');
  const isEdit = !!editId;

  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [authType, setAuthType] = useState('none');
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(isEdit);
  const [discoverResult, setDiscoverResult] = useState<string | null>(null);

  useEffect(() => {
    if (!editId) return;
    // 当前后端没有单条 GET /admin/mcp-servers/{id} 接口,编辑态从列表接口里按 id 找一次
    api.listMcpServers().then((r) => {
      const s = r.items.find((x: any) => x.id === editId);
      if (s) { setName(s.name); setUrl(s.url); setAuthType(s.auth_type); }
    }).finally(() => setLoading(false));
  }, [editId]);

  const handleSave = async () => {
    if (!name.trim() || !url.trim()) { toast.error('请填写名称和 URL'); return; }
    setSaving(true);
    try {
      const created = await api.createMcpServer({ name, url, auth_type: authType });
      toast.success('MCP Server 已添加');
      try {
        const r = await api.discoverMcpTools(created.id);
        setDiscoverResult(`发现并导入 ${r.imported_count} 个工具`);
      } catch {
        setDiscoverResult('已保存，工具发现稍后可在列表页重试');
      }
      setTimeout(() => navigate('/tools/mcp'), 900);
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-center gap-4 mb-2">
        <Button variant="ghost" size="icon" onClick={() => navigate('/tools/mcp')} className="rounded-full">
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">{isEdit ? '编辑 MCP Server' : '接入 MCP Server'}</h1>
          <p className="text-slate-500 text-sm">连接到符合 Model Context Protocol 的服务以获取外部工具能力。</p>
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardContent className="p-6 space-y-6">
          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-700">Server 名称</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：Weather Tools Service" className="h-11 rounded-xl" />
          </div>

          <div className="space-y-2">
            <label className="text-sm font-semibold text-slate-700">Server URL</label>
            <div className="relative">
              <LinkIcon className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
              <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://mcp-server.example.com" className="pl-10 h-11 rounded-xl" />
            </div>
            <div className="flex items-center justify-between text-[11px] text-slate-400">
              <span>地址里可以写 {'{{系统参数}}'},按调用时的景区取值;MCP 工具的入参按参数定义里的「自动填入入参名」补齐。</span>
              <ParamRefPicker />
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-xs font-medium text-slate-500 uppercase tracking-wider flex items-center gap-1.5"><ShieldCheck size={13} />认证方式</label>
            <select value={authType} onChange={(e) => setAuthType(e.target.value)} className="w-full h-11 px-4 rounded-xl border border-slate-200 bg-white text-sm outline-none focus:border-blue-500">
              <option value="none">无认证 (None)</option>
              <option value="api_key">API Key</option>
              <option value="bearer">Bearer Token</option>
              <option value="basic">Basic Auth</option>
            </select>
          </div>

          {discoverResult && (
            <div className="p-3 rounded-xl bg-emerald-50 border border-emerald-100 flex items-center gap-2 text-sm text-emerald-700">
              <Sparkles size={15} />{discoverResult}
            </div>
          )}
        </CardContent>
      </Card>

      <div className="flex gap-4">
        <Button variant="ghost" className="flex-1 h-12 rounded-xl font-bold text-slate-500" onClick={() => navigate('/tools/mcp')}>取消</Button>
        <Button disabled={saving} className="flex-1 h-12 rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-bold shadow-lg shadow-blue-100 gap-2" onClick={handleSave}>
          {saving ? <Loader2 size={18} className="animate-spin" /> : <Rocket size={18} />} 保存并发现工具
        </Button>
      </div>
    </div>
  );
};

export default MCPNew;
