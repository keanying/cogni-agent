import React, { useEffect, useMemo, useState } from 'react';
import { BookOpen, Search, FileCode2, Loader2, ChevronRight, ChevronDown, Clock, FolderOpen } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import * as api from '../../lib/api';
import Markdown from '../../components/common/Markdown';

/**
 * 系统设置 → 系统功能说明。
 *
 * 罗列项目的设计框架与功能逻辑(数据源 = 后端 cogni_agent/feature_docs 下的
 * md 文件,支持子目录;子目录 = 功能域,与代码架构对应,如 scheduling/;
 * 随镜像发布,改文档重新部署即
 * 生效)。左侧按目录分组的折叠列表(文档多了靠目录树定位,不靠文件名猜)+
 * 前端搜索(量小);点开条目 → 右侧渲染完整设计逻辑。文档头部展示 related
 * 关联代码文件——这些文件有行为级改动时,对应文档需在同一次提交里同步更新。
 */
type TreeNode = {
  name: string;
  docs: api.FeatureDocItem[];
  children: TreeNode[];
  total: number;
};

const FeatureDocs: React.FC = () => {
  const [docs, setDocs] = useState<api.FeatureDocItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [kw, setKw] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<api.FeatureDocDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const r = await api.getFeatureDocs();
        setDocs(r.items || []);
        if (r.items?.length) setSelected(r.items[0].id);
      } finally { setLoading(false); }
    })();
  }, []);

  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    setDetailLoading(true);
    api.getFeatureDoc(selected)
      .then(setDetail)
      .finally(() => setDetailLoading(false));
  }, [selected]);

  const toggleDir = (dir: string) => setCollapsed((prev) => {
    const next = new Set(prev);
    next.has(dir) ? next.delete(dir) : next.add(dir);
    return next;
  });

  const filtered = useMemo(() => {
    const k = kw.trim().toLowerCase();
    return docs.filter((d) =>
      !k
      || d.title.toLowerCase().includes(k)
      || d.summary.toLowerCase().includes(k)
      || d.category.toLowerCase().includes(k)
      || d.directory.toLowerCase().includes(k)
      || d.related.join(' ').toLowerCase().includes(k));
  }, [docs, kw]);

  // 目录树(任意层级,构建与渲染均递归——以后加三级四级目录零代码改动):
  // 顶级约定 = 框架/业务/通用(排序固定),根目录文档归「通用」;搜索时自动展开
  const tree = useMemo<TreeNode[]>(() => {
    type RawNode = { name: string; docs: api.FeatureDocItem[]; children: Map<string, RawNode> };
    const node = (m: Map<string, RawNode>, name: string): RawNode => {
      if (!m.has(name)) m.set(name, { name, docs: [], children: new Map() });
      return m.get(name)!;
    };
    const root = new Map<string, RawNode>();
    for (const d of filtered) {
      const segs = d.directory ? d.directory.split('/') : [];
      if (segs.length === 0) { node(root, '通用').docs.push(d); continue; }
      let cur = node(root, segs[0]);
      for (let i = 1; i < segs.length; i++) cur = node(cur.children, segs[i]);
      cur.docs.push(d);
    }
    const finalize = (n: RawNode): TreeNode => {
      const children = Array.from(n.children.values()).map(finalize);
      return { name: n.name, docs: n.docs, children,
        total: n.docs.length + children.reduce((s, c) => s + c.total, 0) };
    };
    const rank = (n: string) => ({ 框架: 0, 业务: 1, 通用: 2 } as Record<string, number>)[n] ?? 3;
    return Array.from(root.values())
      .map(finalize)
      .sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
  }, [filtered]);

  const renderDir = (n: TreeNode, path: string, depth: number) => {
    const open = !collapsed.has(path) || !!kw.trim();
    return (
      <div key={path}>
        <button onClick={() => toggleDir(path)}
          className="flex w-full items-center gap-1.5 px-1.5 py-1 text-left"
          style={{ paddingLeft: 6 + depth * 12 }}>
          {open
            ? <ChevronDown size={13} className="shrink-0 text-slate-400" />
            : <ChevronRight size={13} className="shrink-0 text-slate-400" />}
          {depth === 0
            ? <span className="text-[12px] font-black text-slate-700">{n.name}</span>
            : <>
                <FolderOpen size={12} className="shrink-0 text-amber-500" />
                <span className="text-[11px] font-bold text-slate-600">{n.name}</span>
              </>}
          <span className="text-[10px] text-slate-400">{n.total}</span>
        </button>
        {open && (
          <div className="mt-1 space-y-1.5 border-l border-slate-100" style={{ marginLeft: 6 + depth * 12 }}>
            {n.children.map((c) =>
              renderDir(c, `${path}/${c.name}`, depth + 1))}
            {n.docs.map((d) => (
              <button key={d.id} onClick={() => setSelected(d.id)}
                className={cn('w-full rounded-xl border p-3 text-left transition-all',
                  selected === d.id
                    ? 'border-blue-300 bg-blue-50/60 shadow-sm'
                    : 'border-slate-100 bg-white hover:border-slate-200 hover:bg-slate-50')}>
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-[13px] font-bold text-slate-800">{d.title}</span>
                  <ChevronRight size={13} className={cn('shrink-0',
                    selected === d.id ? 'text-blue-500' : 'text-slate-300')} />
                </div>
                <div className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-slate-500">{d.summary}</div>
                <div className="mt-1.5 flex items-center gap-1.5">
                  <Badge className="bg-slate-100 text-slate-500 border-none text-[10px] font-bold">
                    {d.category}
                  </Badge>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="p-6">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <BookOpen size={18} className="text-slate-500" />
        <h1 className="text-lg font-black text-slate-900">系统功能说明</h1>
        <span className="text-xs text-slate-400">
          项目设计框架与功能逻辑索引 · 内容随代码发布同步更新
        </span>
        <div className="flex-1" />
        {/* 这里是「对内」的设计文档;对外集成方要看的是另一份,且不需要登录 */}
        <a href="/developer" target="_blank" rel="noreferrer"
           className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-blue-600 hover:bg-blue-50">
          <FileCode2 size={13} />开发者 API 文档(对外,免登录)
        </a>
      </div>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>
      ) : docs.length === 0 ? (
        <Card><CardContent className="py-16 text-center text-sm text-slate-400">
          暂无文档(后端 feature_docs 目录为空)
        </CardContent></Card>
      ) : (
        <div className="grid grid-cols-12 gap-4">
          {/* 左:搜索 + 目录分组折叠列表(窄列,主区留给文档内容) */}
          <div className="col-span-3 space-y-3">
            <div className="relative">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
              <Input value={kw} onChange={(e) => setKw(e.target.value)}
                placeholder="搜索功能 / 目录 / 代码文件…" className="h-9 rounded-xl pl-8" />
            </div>
            <div className="space-y-2">
              {tree.map((n) => renderDir(n, n.name, 0))}
              {filtered.length === 0 && (
                <div className="rounded-xl border border-dashed border-slate-200 py-8 text-center text-xs text-slate-400">
                  没有匹配的功能说明
                </div>
              )}
            </div>
          </div>

          {/* 右:详情(主区,留足内容宽度) */}
          <div className="col-span-9">
            <Card className="min-h-[600px] border-slate-200 shadow-sm">
              {detailLoading || !detail ? (
                <CardContent className="flex justify-center py-20">
                  {detailLoading && <Loader2 className="animate-spin text-slate-300" size={28} />}
                </CardContent>
              ) : (
                <>
                  <CardHeader className="border-b bg-slate-50/50 py-4 px-6">
                    <CardTitle className="flex items-center gap-2 text-base font-black text-slate-900">
                      {detail.directory && (
                        <span className="font-mono text-[11px] font-bold text-amber-600 bg-amber-50 rounded px-1.5 py-0.5">
                          {detail.directory}/
                        </span>
                      )}
                      {detail.title}
                      <Badge className="bg-slate-900 text-white border-none text-[10px]">{detail.category}</Badge>
                    </CardTitle>
                    <div className="mt-1 flex items-center gap-4 text-[11px] text-slate-400">
                      {detail.updated_at > 0 && (
                        <span className="flex items-center gap-1">
                          <Clock size={11} /> 更新于 {new Date(detail.updated_at * 1000).toLocaleDateString()}
                        </span>
                      )}
                      {detail.related.length > 0 && (
                        <span className="flex items-center gap-1 truncate">
                          <FileCode2 size={11} className="shrink-0" />
                          关联代码:
                          {detail.related.map((f) => (
                            <code key={f}
                              className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-600">
                              {f.split('/').slice(-2).join('/')}
                            </code>
                          ))}
                        </span>
                      )}
                    </div>
                  </CardHeader>
                  <CardContent className="p-6">
                    <Markdown content={detail.content} />
                  </CardContent>
                </>
              )}
            </Card>
          </div>
        </div>
      )}
    </div>
  );
};

export default FeatureDocs;
