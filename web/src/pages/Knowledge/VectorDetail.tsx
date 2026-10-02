
import React, { useEffect, useState } from 'react';
import {
  ArrowLeft, Search, FileUp, Trash2, Activity, CheckCircle2, FileText, Database, Sparkles, Loader2, RefreshCw,
} from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../../components/ui/tabs';
import { Input } from '../../components/ui/input';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const VectorDetail: React.FC = () => {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const id = params.get('id');
  const [kb, setKb] = useState<any>(null);
  const [docs, setDocs] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [showUpload, setShowUpload] = useState(false);
  const [uploadName, setUploadName] = useState('');
  const [uploadText, setUploadText] = useState('');
  const [uploading, setUploading] = useState(false);
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<any[] | null>(null);

  const load = async () => {
    if (!id) { setLoading(false); return; }
    setLoading(true);
    try {
      const [all, d] = await Promise.all([api.listKnowledgeBases(), api.listKbDocuments(id)]);
      setKb(all.items.find((k: any) => k.id === id));
      setDocs(d.items);
    } catch (e: any) { toast.error(e.message || '加载失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, [id]);

  const handleUpload = async () => {
    if (!uploadName.trim() || !uploadText.trim() || !id) { toast.error('请填写文件名和文本内容'); return; }
    setUploading(true);
    try {
      const r: any = await api.ingestDocument(id, { file_name: uploadName, text: uploadText });
      if (r.status === 'failed') toast.error(r.error || '入库失败（可能未配置 knowledge.embedding_model）');
      else toast.success(`入库成功，切分 ${r.chunk_count} 个分块`);
      setUploadName(''); setUploadText(''); setShowUpload(false);
      load();
    } catch (e: any) {
      toast.error(e.message || '入库失败');
    } finally {
      setUploading(false);
    }
  };

  const handleSearch = async () => {
    if (!query.trim() || !id) return;
    setSearching(true);
    setSearchResults(null);
    try { setSearchResults((await api.retrieveTest(id, query)).items); }
    catch (e: any) { toast.error(e.message || '检索失败（可能未配置 knowledge.embedding_model）'); setSearchResults([]); }
    finally { setSearching(false); }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  if (!kb) return <div className="text-center py-20 text-slate-400">未找到该知识库</div>;

  return (
    <div className="max-w-7xl mx-auto space-y-6 animate-in fade-in duration-500">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => navigate('/knowledge/vector')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-black text-slate-900 tracking-tight">{kb.name}</h1>
              <Badge className="bg-purple-500 text-white border-none shadow-sm font-black tracking-widest text-[10px]">READY</Badge>
            </div>
          </div>
        </div>
        <Button className="bg-slate-900 hover:bg-black text-white rounded-xl h-10 px-6 font-black shadow-lg shadow-slate-200 gap-2" onClick={() => setShowUpload(!showUpload)}>
          <FileUp size={16} /> 上传文档
        </Button>
      </div>

      {showUpload && (
        <Card className="border-blue-100 bg-blue-50/30 shadow-sm">
          <CardContent className="p-5 space-y-3">
            <Input value={uploadName} onChange={(e) => setUploadName(e.target.value)} placeholder="文件名，如 gulangyu.txt" />
            <textarea value={uploadText} onChange={(e) => setUploadText(e.target.value)} rows={5} placeholder="粘贴文本内容（复杂文件格式需先自行抽取文本）..."
              className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none" />
            <Button disabled={uploading} onClick={handleUpload} className="bg-[#1677ff] gap-2">{uploading ? <Loader2 size={14} className="animate-spin" /> : <FileUp size={14} />}切分并入库</Button>
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        <div className="lg:col-span-8 space-y-6">
          <Tabs defaultValue="documents" className="w-full">
            <TabsList className="bg-slate-100/50 p-1 border rounded-xl gap-1">
              <TabsTrigger value="documents" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-black gap-2 px-6 py-2 text-xs uppercase tracking-widest">文档管理 ({docs.length})</TabsTrigger>
              <TabsTrigger value="search" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-black gap-2 px-6 py-2 text-xs uppercase tracking-widest">检索测试 (RAG Lab)</TabsTrigger>
            </TabsList>

            <div className="mt-6">
              <TabsContent value="documents" className="space-y-4">
                <div className="grid grid-cols-1 gap-3">
                  {docs.map((doc) => (
                    <Card key={doc.id} className="group border-slate-200 shadow-sm bg-white hover:border-blue-200 transition-all overflow-hidden">
                      <CardContent className="p-4 flex items-center justify-between">
                        <div className="flex items-center gap-4">
                          <div className="h-10 w-10 rounded-xl bg-slate-50 border border-slate-100 flex items-center justify-center text-slate-400 group-hover:bg-blue-50 group-hover:text-blue-500 transition-colors"><FileText size={20} /></div>
                          <div>
                            <div className="text-sm font-bold text-slate-800">{doc.file_name}</div>
                            <div className="text-[10px] text-slate-400 font-medium">{new Date(doc.created_at).toLocaleString()}</div>
                          </div>
                        </div>
                        <div className="flex items-center gap-1.5">
                          {doc.status === 'indexed' ? <CheckCircle2 size={14} className="text-emerald-500" /> : doc.status === 'failed' ? <Badge className="bg-rose-50 text-rose-600 border-none text-[10px]">失败</Badge> : <Loader2 size={14} className="animate-spin text-blue-400" />}
                          <span className="text-[10px] font-black uppercase tracking-tighter text-slate-500">{doc.status}</span>
                        </div>
                      </CardContent>
                    </Card>
                  ))}
                  {docs.length === 0 && <div className="text-center text-sm text-slate-300 py-10">暂无文档</div>}
                </div>
              </TabsContent>

              <TabsContent value="search" className="space-y-6">
                <Card className="border-slate-200 shadow-sm overflow-hidden bg-white">
                  <CardContent className="p-6 space-y-4">
                    <div className="relative">
                      <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={18} />
                      <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="输入查询语句测试检索效果..." className="pl-11 h-12 rounded-xl border-slate-200 shadow-sm" onKeyDown={(e) => e.key === 'Enter' && handleSearch()} />
                      <Button size="sm" onClick={handleSearch} className="absolute right-2 top-1/2 -translate-y-1/2 h-8 rounded-lg bg-blue-600 font-bold px-4">
                        {searching ? <RefreshCw className="h-4 w-4 animate-spin" /> : '检索'}
                      </Button>
                    </div>
                    <div className="space-y-4 mt-6">
                      <h5 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] flex items-center gap-2"><Activity size={12} /> 召回结果（真实向量检索）</h5>
                      {searchResults && searchResults.length > 0 ? (
                        <div className="space-y-4">
                          {searchResults.map((res, i) => (
                            <div key={i} className="p-4 rounded-2xl bg-slate-50 border border-slate-100 space-y-3">
                              <div className="flex items-center gap-1 text-[10px] font-bold text-slate-400 uppercase"><FileText size={10} /> {res.type || res.source || 'chunk'}</div>
                              <p className="text-sm text-slate-700 leading-relaxed font-medium">{typeof res === 'string' ? res : JSON.stringify(res)}</p>
                            </div>
                          ))}
                        </div>
                      ) : searchResults && searchResults.length === 0 ? (
                        <div className="text-center text-xs text-slate-300 py-10">未召回相关分块</div>
                      ) : !searching && (
                        <div className="h-[200px] flex flex-col items-center justify-center text-slate-300 gap-3 border-2 border-dashed rounded-2xl border-slate-100">
                          <Sparkles size={32} strokeWidth={1} /><span className="text-xs font-bold uppercase tracking-widest">输入查询语句开始测试</span>
                        </div>
                      )}
                    </div>
                  </CardContent>
                </Card>
              </TabsContent>
            </div>
          </Tabs>
        </div>

        <div className="lg:col-span-4 space-y-6">
          <Card className="border-slate-200 shadow-sm overflow-hidden">
            <CardHeader className="border-b bg-slate-50/50 py-4 px-6"><CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600"><Database size={16} className="text-blue-600" /> 引擎规格</CardTitle></CardHeader>
            <CardContent className="p-6 space-y-4">
              <div className="flex items-center justify-between p-3 rounded-xl border border-slate-100 bg-slate-50/50"><div className="text-xs font-bold text-slate-500 uppercase">向量引擎</div><div className="text-sm font-black text-slate-900 uppercase">pgvector</div></div>
              <div className="flex items-center justify-between p-3 rounded-xl border border-slate-100 bg-slate-50/50"><div className="text-xs font-bold text-slate-500 uppercase">可见范围</div><div className="text-sm font-black text-slate-900 uppercase">{kb.visibility}</div></div>
              <p className="text-[11px] text-slate-400 pt-2">切分策略：滑窗切分（500 字符/50 重叠）；嵌入模型由 config.yaml: knowledge.embedding_model 统一配置。</p>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
};

export default VectorDetail;
