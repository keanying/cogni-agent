
import React, { useEffect, useState } from 'react';
import { ArrowLeft, Info, Cloud, Database, Loader2 } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const VectorNew: React.FC = () => {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [scope, setScope] = useState('group');
  const [storageConfigs, setStorageConfigs] = useState<any[]>([]);
  const [vectorDbConfigs, setVectorDbConfigs] = useState<any[]>([]);
  const [storageConfigId, setStorageConfigId] = useState('');
  const [vectorDbConfigId, setVectorDbConfigId] = useState('');
  const [newStorageEndpoint, setNewStorageEndpoint] = useState('http://minio-internal:9000');
  const [newStorageBucket, setNewStorageBucket] = useState('knowledge-base');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    Promise.all([api.listVectorStorageConfigs(), api.listVectorDbConfigs()]).then(([s, v]) => {
      setStorageConfigs(s.items); setVectorDbConfigs(v.items);
      if (v.items.length) setVectorDbConfigId(v.items[0].id);
    }).catch(() => {});
  }, []);

  const handleSave = async () => {
    if (!name.trim()) { toast.error('请填写知识库名称'); return; }
    setSaving(true);
    try {
      let scId = storageConfigId;
      if (!scId) {
        const created = await api.createVectorStorageConfig({ name: `${name}-storage`, endpoint: newStorageEndpoint, bucket: newStorageBucket });
        scId = created.id;
      }
      let vdId = vectorDbConfigId;
      if (!vdId) {
        const created = await api.createVectorDbConfig({ name: `${name}-vectordb`, backend: 'pgvector', connection_config: {} });
        vdId = created.id;
      }
      await api.createKnowledgeBase({ name, storage_config_id: scId, vector_db_config_id: vdId, scope, visibility: scope });
      toast.success('向量知识库已成功创建');
      navigate('/knowledge/vector');
    } catch (e: any) {
      toast.error(e.message || '创建失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex items-center gap-4 mb-2">
        <Button variant="ghost" size="icon" onClick={() => navigate('/knowledge/vector')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">新建向量知识库</h1>
          <p className="text-slate-500 text-sm">配置存储与向量引擎，实现语义检索能力。</p>
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardHeader className="border-b bg-slate-50/50 py-3 px-6"><CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500"><Info size={16} /> 基础信息</CardTitle></CardHeader>
        <CardContent className="p-6 space-y-6">
          <div className="grid grid-cols-2 gap-6">
            <div className="space-y-2">
              <label className="text-sm font-semibold text-slate-700">知识库名称</label>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：武夷山旅游百科知识库" className="h-11 rounded-xl" />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-semibold text-slate-700">可见范围</label>
              <select value={scope} onChange={(e) => setScope(e.target.value)} className="w-full h-11 px-4 rounded-xl border border-slate-200 bg-white text-sm outline-none focus:border-blue-500">
                <option value="group">集团内部 (Group)</option><option value="scenic">本景区专属 (Scenic)</option><option value="public">全平台公开 (Public)</option>
              </select>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardHeader className="border-b bg-slate-50/50 py-3 px-6"><CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500"><Cloud size={16} className="text-blue-500" /> 对象存储配置</CardTitle></CardHeader>
        <CardContent className="p-6 space-y-4">
          <select value={storageConfigId} onChange={(e) => setStorageConfigId(e.target.value)} className="w-full h-11 px-4 rounded-xl border border-slate-200 bg-white text-sm outline-none">
            <option value="">+ 新建存储配置</option>
            {storageConfigs.map((s) => <option key={s.id} value={s.id}>{s.name}（{s.endpoint}）</option>)}
          </select>
          {!storageConfigId && (
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2"><label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">Endpoint URL</label><Input value={newStorageEndpoint} onChange={(e) => setNewStorageEndpoint(e.target.value)} className="h-11 rounded-xl font-mono text-xs" /></div>
              <div className="space-y-2"><label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">Bucket</label><Input value={newStorageBucket} onChange={(e) => setNewStorageBucket(e.target.value)} className="h-11 rounded-xl" /></div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardHeader className="border-b bg-slate-50/50 py-3 px-6"><CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500"><Database size={16} className="text-purple-500" /> 向量数据库配置</CardTitle></CardHeader>
        <CardContent className="p-6 space-y-4">
          <select value={vectorDbConfigId} onChange={(e) => setVectorDbConfigId(e.target.value)} className="w-full h-11 px-4 rounded-xl border border-slate-200 bg-white text-sm outline-none">
            <option value="">+ 新建向量库配置（默认 pgvector）</option>
            {vectorDbConfigs.map((v) => <option key={v.id} value={v.id}>{v.name}（{v.backend}）</option>)}
          </select>
          <p className="text-[11px] text-slate-400">当前系统默认向量后端为 pgvector（复用主 Postgres 数据库），嵌入模型由 config.yaml: knowledge.embedding_model 统一配置。</p>
        </CardContent>
      </Card>

      <div className="flex gap-4 pt-4 pb-12">
        <Button variant="ghost" className="flex-1 h-14 rounded-2xl font-black text-slate-500 uppercase tracking-widest" onClick={() => navigate('/knowledge/vector')}>取消并返回</Button>
        <Button disabled={saving} className="flex-1 h-14 rounded-2xl bg-slate-900 hover:bg-black text-white font-black text-lg shadow-2xl shadow-slate-200 gap-3" onClick={handleSave}>
          {saving ? <Loader2 className="animate-spin" /> : null} 创建向量知识库
        </Button>
      </div>
    </div>
  );
};

export default VectorNew;
