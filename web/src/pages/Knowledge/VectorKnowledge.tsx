import React, { useEffect, useRef, useState } from 'react';
import {
  Boxes, Plus, Upload, ClipboardPaste, Database, Loader2, Search, FileText,
  CheckCircle2, XCircle, AlertTriangle,
} from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { Badge } from '../../components/ui/badge';
import { FieldLabel, Tutorial } from '../../components/common/FormBits';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { promptDialog } from '../../components/ui/confirm';

/**
 * 向量知识。三件事:接向量库、维护知识库与文档、检索自测。
 *
 * 入库方式做了三种,因为实际场景差别很大:
 * - **粘贴文本**:临时补一段说明、一份 FAQ,最快
 * - **上传文件**:已有的 md/txt/csv 等文本文件,批量拖进来
 * - **接已有 collection**:Milvus 里已经有别的系统灌好的向量,直接用,不重复灌
 */

const TEXT_EXT = ['.txt', '.md', '.markdown', '.csv', '.json', '.log', '.yaml', '.yml', '.html'];

const VectorKnowledge: React.FC = () => {
  const [tab, setTab] = useState<'bases' | 'backend'>('bases');

  // ── 知识库与文档 ──
  const [bases, setBases] = useState<any[]>([]);
  const [activeKb, setActiveKb] = useState<string>('');
  const [docs, setDocs] = useState<api.KbDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [ingesting, setIngesting] = useState(false);
  const [ingestMode, setIngestMode] = useState<'paste' | 'upload'>('paste');
  const [pasteName, setPasteName] = useState('');
  const [pasteText, setPasteText] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  // ── 检索自测 ──
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<any[] | null>(null);
  const [searching, setSearching] = useState(false);

  // ── 向量库连接 ──
  const [backend, setBackend] = useState('milvus');
  const [conn, setConn] = useState<Record<string, any>>({
    uri: 'http://localhost:19530', token: '', db_name: 'default',
    collection: 'zhilv_chunks', dim: 768, metric: 'COSINE', index_type: 'HNSW',
  });
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);

  const loadBases = async () => {
    setLoading(true);
    try {
      const r = await api.listKnowledgeBases();
      setBases(r.items);
      if (r.items.length && !activeKb) setActiveKb(r.items[0].id);
    } catch (e: any) { toast.error(e.message || '加载知识库失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { loadBases(); }, []);
  useEffect(() => {
    if (!activeKb) { setDocs([]); return; }
    api.listKbDocuments(activeKb).then((r) => setDocs(r.items)).catch(() => setDocs([]));
  }, [activeKb]);

  const newBase = async () => {
    const name = await promptDialog({
      title: '新建知识库',
      message: '同一个知识库里的文档共用一份向量索引,检索时按库隔离。',
      confirmLabel: '创建',
      input: { label: '知识库名称', placeholder: '如:景区规章制度', required: true },
    });
    if (!name?.trim()) return;
    try {
      const r = await api.createKnowledgeBase({ name: name.trim() });
      toast.success('知识库已创建');
      await loadBases();
      setActiveKb(r.id);
    } catch (e: any) { toast.error(e.message || '创建失败'); }
  };

  const doIngest = async (fileName: string, text: string) => {
    if (!activeKb) { toast.error('请先选择或创建一个知识库'); return; }
    if (!text.trim()) { toast.error('内容为空'); return; }
    setIngesting(true);
    try {
      const r = await api.ingestDocument(activeKb, { file_name: fileName, text });
      toast.success(`已入库,切分为 ${r.chunk_count} 个片段`);
      setPasteText(''); setPasteName('');
      api.listKbDocuments(activeKb).then((x) => setDocs(x.items));
    } catch (e: any) {
      // 嵌入模型没配时后端会返回一段可操作的说明,原样透出而不是盖成"入库失败"
      toast.error(e.message || '入库失败', { duration: 8000 });
    } finally { setIngesting(false); }
  };

  const onFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    for (const f of Array.from(files)) {
      const ok = TEXT_EXT.some((ext) => f.name.toLowerCase().endsWith(ext));
      if (!ok) {
        // 明确说清楚为什么不支持,而不是静默跳过
        toast.error(`${f.name}:暂只支持文本类文件(${TEXT_EXT.join(' ')})。PDF/Word 需要先转成文本。`);
        continue;
      }
      await doIngest(f.name, await f.text());
    }
    if (fileRef.current) fileRef.current.value = '';
  };

  const runSearch = async () => {
    if (!activeKb || !query.trim()) return;
    setSearching(true);
    try { setHits((await api.retrieveTest(activeKb, query.trim())).items); }
    catch (e: any) { toast.error(e.message || '检索失败', { duration: 8000 }); setHits([]); }
    finally { setSearching(false); }
  };

  const saveBackend = async () => {
    try {
      await api.createVectorDbConfig({ name: backend, backend, connection_config: conn });
      toast.success('向量库配置已保存');
    } catch (e: any) { toast.error(e.message || '保存失败'); }
  };
  const testBackend = async () => {
    setTesting(true); setTestResult(null);
    try { setTestResult(await api.testVectorDbConfig()); }
    catch (e: any) { setTestResult({ ok: false, detail: e.message || '连接失败' }); }
    finally { setTesting(false); }
  };

  return (
    <div className="mx-auto max-w-6xl space-y-5 pb-12">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-bold text-slate-900">
          <Boxes size={20} className="text-purple-500" />向量知识
        </h1>
        <p className="mt-1 text-xs text-slate-500">
          文档切分后转成向量存入向量库,Skill 启用「vector」知识源即可语义检索。
        </p>
      </div>

      <div className="flex gap-2 border-b border-slate-100">
        {([['bases', '知识库与文档'], ['backend', '向量库连接']] as const).map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)}
            className={cn('px-4 py-2.5 text-sm font-medium transition-colors',
              tab === k ? 'border-b-2 border-blue-500 text-blue-600' : 'text-slate-400 hover:text-slate-600')}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'bases' ? (
        <div className="grid grid-cols-12 gap-4">
          {/* 左:知识库列表 */}
          <div className="col-span-3 space-y-2">
            <Button onClick={newBase} variant="outline" className="w-full gap-1.5 text-sm">
              <Plus size={14} />新建知识库
            </Button>
            {loading ? (
              <div className="py-8 text-center"><Loader2 className="mx-auto animate-spin text-slate-300" size={20} /></div>
            ) : bases.length === 0 ? (
              <p className="py-8 text-center text-xs text-slate-300">还没有知识库</p>
            ) : bases.map((b) => (
              <button key={b.id} onClick={() => { setActiveKb(b.id); setHits(null); }}
                className={cn('w-full rounded-xl border px-3 py-2.5 text-left transition-all',
                  activeKb === b.id ? 'border-blue-200 bg-blue-50' : 'border-transparent hover:bg-slate-50')}>
                <div className="truncate text-[13px] font-semibold text-slate-800">{b.name}</div>
                <div className="mt-0.5 text-[10px] text-slate-400">{b.description || '—'}</div>
              </button>
            ))}
          </div>

          {/* 中:入库 + 文档列表 */}
          <div className="col-span-5 space-y-4">
            <Card className="border-slate-200 shadow-sm">
              <div className="flex gap-1 border-b border-slate-100 bg-slate-50/60 px-4 py-2">
                {([['paste', '粘贴文本', ClipboardPaste], ['upload', '上传文件', Upload]] as const).map(([k, label, Icon]) => (
                  <button key={k} onClick={() => setIngestMode(k)}
                    className={cn('flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12px] font-medium transition-colors',
                      ingestMode === k ? 'bg-white text-blue-600 shadow-sm' : 'text-slate-500 hover:text-slate-700')}>
                    <Icon size={13} />{label}
                  </button>
                ))}
              </div>
              <CardContent className="space-y-3 p-4">
                {ingestMode === 'paste' ? (
                  <>
                    <div>
                      <FieldLabel label="文档名称" hint="用于在文档列表里区分来源,建议写清楚出处" />
                      <Input value={pasteName} onChange={(e) => setPasteName(e.target.value)} placeholder="如:2026 年门票政策" />
                    </div>
                    <div>
                      <FieldLabel label="文本内容" hint="直接粘贴。系统会按段落自动切分成片段再转向量。" />
                      <textarea value={pasteText} onChange={(e) => setPasteText(e.target.value)} rows={10}
                        placeholder="粘贴要入库的文本…"
                        className="w-full rounded-xl border border-slate-200 p-3 text-[13px] leading-relaxed outline-none focus:border-blue-400" />
                      <div className="mt-1 text-right text-[10px] text-slate-400">{pasteText.length} 字</div>
                    </div>
                    <Button className="w-full gap-1.5 bg-[#1677ff]" disabled={ingesting || !activeKb}
                            onClick={() => doIngest(pasteName.trim() || '粘贴内容', pasteText)}>
                      {ingesting ? <Loader2 size={14} className="animate-spin" /> : <ClipboardPaste size={14} />}入库
                    </Button>
                  </>
                ) : (
                  <div
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => { e.preventDefault(); onFiles(e.dataTransfer.files); }}
                    onClick={() => fileRef.current?.click()}
                    className="cursor-pointer rounded-xl border-2 border-dashed border-slate-200 py-12 text-center transition-colors hover:border-blue-300 hover:bg-blue-50/30">
                    <Upload size={28} className="mx-auto mb-2 text-slate-300" />
                    <p className="text-sm font-medium text-slate-600">点击选择,或把文件拖到这里</p>
                    <p className="mt-1 text-[11px] text-slate-400">支持 {TEXT_EXT.join(' ')},可多选</p>
                    <p className="mt-2 text-[10px] text-slate-300">PDF / Word 需要先转成文本再上传</p>
                    <input ref={fileRef} type="file" multiple hidden accept={TEXT_EXT.join(',')}
                           onChange={(e) => onFiles(e.target.files)} />
                  </div>
                )}
              </CardContent>
            </Card>

            <Card className="border-slate-200 shadow-sm">
              <div className="border-b border-slate-100 bg-slate-50/60 px-4 py-2.5 text-[13px] font-bold text-slate-700">
                已入库文档({docs.length})
              </div>
              <CardContent className="p-0">
                {docs.length === 0 ? (
                  <p className="py-10 text-center text-xs text-slate-300">该知识库还没有文档</p>
                ) : (
                  <div className="divide-y divide-slate-50">
                    {docs.map((d) => (
                      <div key={d.id} className="flex items-center gap-3 px-4 py-2.5">
                        <FileText size={14} className="shrink-0 text-slate-300" />
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-[13px] text-slate-700">{d.file_name}</div>
                          <div className="text-[10px] text-slate-400">{d.chunk_count} 个片段</div>
                        </div>
                        <Badge variant="outline" className={cn('shrink-0 text-[10px]',
                          d.status === 'indexed' ? 'border-emerald-100 bg-emerald-50 text-emerald-600'
                            : d.status === 'failed' ? 'border-rose-100 bg-rose-50 text-rose-600'
                            : 'bg-slate-100 text-slate-500')}>
                          {d.status === 'indexed' ? '已索引' : d.status === 'failed' ? '失败' : '处理中'}
                        </Badge>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>

          {/* 右:检索自测 */}
          <div className="col-span-4">
            <Card className="sticky top-4 border-slate-200 shadow-sm">
              <div className="flex items-center gap-1.5 border-b border-slate-100 bg-slate-50/60 px-4 py-2.5">
                <Search size={14} className="text-slate-500" />
                <span className="text-[13px] font-bold text-slate-700">检索自测</span>
              </div>
              <CardContent className="space-y-3 p-4">
                <p className="text-[11px] leading-relaxed text-slate-400">
                  入库之后立刻验证能不能召回,而不是等对话里发现检索不到东西。
                </p>
                <div className="flex gap-2">
                  <Input value={query} onChange={(e) => setQuery(e.target.value)}
                         onKeyDown={(e) => e.key === 'Enter' && runSearch()}
                         placeholder="输入一个问题…" className="text-sm" />
                  <Button size="sm" onClick={runSearch} disabled={searching || !activeKb} className="shrink-0 bg-[#1677ff]">
                    {searching ? <Loader2 size={13} className="animate-spin" /> : <Search size={13} />}
                  </Button>
                </div>
                {hits === null ? (
                  <div className="rounded-lg border-2 border-dashed border-slate-100 py-8 text-center text-[11px] text-slate-300">
                    输入问题后回车试试
                  </div>
                ) : hits.length === 0 ? (
                  <div className="rounded-lg border border-amber-100 bg-amber-50 p-3 text-[11px] leading-relaxed text-amber-700">
                    <AlertTriangle size={12} className="mr-1 inline" />
                    没有召回任何内容。常见原因:该知识库还没有文档、嵌入模型未配置、
                    或问题与已入库内容确实无关。
                  </div>
                ) : (
                  <div className="space-y-2">
                    {hits.map((h, i) => (
                      <div key={i} className="rounded-lg border border-slate-100 bg-slate-50/60 p-2.5">
                        <div className="mb-1 flex items-center justify-between">
                          <span className="text-[10px] font-semibold text-slate-400">#{i + 1}</span>
                          {h.score != null && (
                            <span className="font-mono text-[10px] text-blue-600">相似度 {Number(h.score).toFixed(3)}</span>
                          )}
                        </div>
                        <p className="line-clamp-4 text-[11.5px] leading-relaxed text-slate-600">
                          {h.content || h.text || h.content_ref || JSON.stringify(h).slice(0, 200)}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      ) : (
        /* ── 向量库连接 ── */
        <div className="grid grid-cols-12 gap-4">
          <div className="col-span-7">
            <Card className="border-slate-200 shadow-sm">
              <CardContent className="space-y-4 p-6">
                <div>
                  <FieldLabel label="向量库类型" required
                    hint="Milvus 用于生产;pgvector 复用元数据库,适合小规模或对比召回效果。" />
                  <div className="flex gap-2">
                    {([['milvus', 'Milvus'], ['pgvector', 'pgvector(内置)']] as const).map(([k, label]) => (
                      <button key={k} onClick={() => setBackend(k)}
                        className={cn('flex-1 rounded-xl border py-2.5 text-sm font-medium transition-all',
                          backend === k ? 'border-blue-200 bg-blue-50 text-blue-600' : 'border-slate-200 text-slate-500 hover:bg-slate-50')}>
                        {label}
                      </button>
                    ))}
                  </div>
                </div>

                {backend === 'milvus' ? (
                  <>
                    <div>
                      <FieldLabel label="连接地址" required hint="Milvus 服务地址。Zilliz Cloud 用它给的 endpoint。" />
                      <Input value={conn.uri} onChange={(e) => setConn({ ...conn, uri: e.target.value })}
                             placeholder="http://localhost:19530" className="font-mono text-sm" />
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <FieldLabel label="Token" hint="Zilliz Cloud 必填;自建 Milvus 未开鉴权时留空。加密存储。" />
                        <Input type="password" value={conn.token} onChange={(e) => setConn({ ...conn, token: e.target.value })}
                               placeholder="自建未开鉴权可留空" />
                      </div>
                      <div>
                        <FieldLabel label="数据库" hint="Milvus 2.4+ 的 database 概念,默认 default" />
                        <Input value={conn.db_name} onChange={(e) => setConn({ ...conn, db_name: e.target.value })} />
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <FieldLabel label="Collection" required
                          hint="**可以填已有的 collection 名**直接接入(需字段结构匹配);不存在时系统会自动创建。" />
                        <Input value={conn.collection} onChange={(e) => setConn({ ...conn, collection: e.target.value })}
                               className="font-mono text-sm" />
                      </div>
                      <div>
                        <FieldLabel label="向量维度" required
                          hint="必须与嵌入模型输出维度一致。bge-m3 是 1024,text-embedding-3-small 是 1536。填错会导致写入失败。" />
                        <Input type="number" value={conn.dim} onChange={(e) => setConn({ ...conn, dim: Number(e.target.value) })} />
                      </div>
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <FieldLabel label="距离度量" hint="COSINE 适合文本嵌入;IP 适合已归一化向量;L2 适合图像。" />
                        <select value={conn.metric} onChange={(e) => setConn({ ...conn, metric: e.target.value })}
                                className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none focus:border-blue-400">
                          {['COSINE', 'IP', 'L2'].map((m) => <option key={m}>{m}</option>)}
                        </select>
                      </div>
                      <div>
                        <FieldLabel label="索引类型" hint="HNSW 查询快、内存占用高;IVF_FLAT 省内存但慢一些。" />
                        <select value={conn.index_type} onChange={(e) => setConn({ ...conn, index_type: e.target.value })}
                                className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none focus:border-blue-400">
                          {['HNSW', 'IVF_FLAT', 'FLAT'].map((m) => <option key={m}>{m}</option>)}
                        </select>
                      </div>
                    </div>
                  </>
                ) : (
                  <div className="rounded-xl border border-slate-100 bg-slate-50 p-4 text-[12px] leading-relaxed text-slate-500">
                    pgvector 复用元数据库(PostgreSQL),不需要额外连接配置。
                    适合数据量不大、或想和 Milvus 对比召回效果的场景。
                  </div>
                )}

                <div className="flex gap-3 pt-2">
                  <Button variant="outline" className="flex-1 gap-1.5" onClick={testBackend} disabled={testing}>
                    {testing ? <Loader2 size={14} className="animate-spin" /> : <Database size={14} />}测试连接
                  </Button>
                  <Button className="flex-1 bg-[#1677ff]" onClick={saveBackend}>保存配置</Button>
                </div>

                {testResult && (
                  <div className={cn('flex items-start gap-2 rounded-xl p-3 text-[12px] leading-relaxed',
                    testResult.ok ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700')}>
                    {testResult.ok ? <CheckCircle2 size={14} className="mt-0.5 shrink-0" />
                                   : <XCircle size={14} className="mt-0.5 shrink-0" />}
                    <span>{testResult.detail}</span>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>

          <div className="col-span-5">
            <Tutorial items={[
              { title: '接入已有 collection',
                body: '把 Collection 填成 Milvus 里已存在的名字即可直接使用,不会重新灌数据。前提是字段结构匹配:需要有 chunk_id(主键)、kb_id、scenic_id、embedding 四个字段。结构不符时测试连接会明确报错。' },
              { title: '向量维度必须对齐',
                body: '这里的维度要和「系统设置 · 模型与预算」里勾选为嵌入模型的那个模型输出维度一致。不一致时写入会失败——而且是写的时候才失败,不是配的时候,所以建议配完立刻用「测试连接」验证。' },
              { title: '租户隔离怎么做的',
                body: '每条向量都带 scenic_id 标量字段,检索时强制过滤「本景区 or 集团级」。这个过滤条件由后端统一拼接,调用方无法绕过——向量库的租户隔离做漏了就是跨景区数据泄露,所以做了三道防线。' },
              { title: 'PDF / Word 怎么办',
                body: '目前只支持文本类文件。PDF/Word 需要先转成 txt 或 md 再上传。这是刻意的取舍:在服务端做文档解析要引入一堆重依赖(且解析质量参差),不如让你用更专业的工具转好再进来。' },
            ]} />
          </div>
        </div>
      )}
    </div>
  );
};

export default VectorKnowledge;
