import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowLeft, Settings2, Layers, Wrench, ShieldCheck, BookOpen, Loader2,
  Copy, Check, ChevronRight, Clock, Braces,
} from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { CopyId } from '../../components/ui/PageHelpers';
import { useSearchParams } from 'react-router-dom';
import { useModalNavigate } from '../../lib/modalNav';
import { useInRouteModal } from '../../components/common/RouteModal';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { cn } from '../../lib/utils';

const VIS_LABEL: Record<string, string> = { private: '仅创建者', scenic: '本景区', group: '集团内', public: '全平台' };

const STATUS_META: Record<string, { label: string; cls: string; sub: string }> = {
  published: { label: '已发布', cls: 'bg-emerald-50 text-emerald-600 border-emerald-100', sub: '可被 Agent 绑定' },
  draft: { label: '草稿', cls: 'bg-amber-50 text-amber-600 border-amber-100', sub: '编辑中 · 未发布' },
  offline: { label: '已下线', cls: 'bg-slate-100 text-slate-500 border-slate-200', sub: '下线后不再被解析' },
};

/** 版本排序:draft 沉底,其余按 version 降序 —— 列表第一个即「默认选中」的最新版本 */
function sortVersions(items: any[]) {
  return [...items].sort((a, b) => {
    const da = a.status === 'draft' ? 1 : 0;
    const db = b.status === 'draft' ? 1 : 0;
    if (da !== db) return da - db;
    return (b.version ?? 0) - (a.version ?? 0);
  });
}

const SkillDetail: React.FC = () => {
  const navigate = useModalNavigate(); // 在弹窗里打开时,跳详情/编辑留在弹窗、跳回列表即关闭
  // 弹窗里:顶部固定,左侧版本列表 / 中间详情 / 右侧信息栏各自固定高度、各自滚动
  const inModal = useInRouteModal();
  const [params] = useSearchParams();
  const id = params.get('id');

  const [skill, setSkill] = useState<any>(null);
  const [versions, setVersions] = useState<any[]>([]);
  const [toolMap, setToolMap] = useState<Record<string, any>>({});
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);

  // 加载 skill 本体 + 工具名映射(用于渲染绑定列表的名称/类型)
  const load = async () => {
    if (!id) { setLoading(false); return; }
    setLoading(true);
    try {
      const [s, tools] = await Promise.all([
        api.getSkill(id),
        api.listTools().catch(() => null),
      ]);
      setSkill(s);
      const map: Record<string, any> = {};
      (tools?.items || []).forEach((t: any) => { map[t.id] = t; });
      setToolMap(map);
    } catch (e: any) {
      toast.error(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, [id]);

  // 版本列表:URL version_id 优先,否则默认选中第一个(最新版本);保留用户手动选择
  useEffect(() => {
    if (!id) return;
    api.listResourceVersions('skill', id)
      .then((r) => {
        const items = r.items || [];
        const sorted = sortVersions(items);
        setVersions(items);
        const urlV = params.get('version_id');
        setSelectedVersionId((prev) => {
          if (urlV && items.some((v: any) => v.id === urlV)) return urlV;
          if (prev && items.some((v: any) => v.id === prev)) return prev;
          return sorted[0]?.id ?? null;
        });
      })
      .catch(() => {});
  }, [id]);

  const viewingVersion = useMemo(
    () => versions.find((v: any) => v.id === selectedVersionId) || null,
    [versions, selectedVersionId],
  );

  // 快照优先:viewingVersion.snapshot 覆盖主表同名字段;无版本记录时回退主表(未发布态)
  const viewSkill = useMemo(() => {
    const snap = viewingVersion?.snapshot || {};
    const rawTools = snap.tools ?? (skill?.tool_ids || []).map((tid: string) => ({ tool_id: tid, version: null }));
    return {
      ...skill,
      ...snap,
      tools: rawTools || [],
      tool_usage: snap.tool_usage ?? skill?.tool_usage ?? {},
      constraints: snap.constraints ?? skill?.constraints ?? {},
    };
  }, [skill, viewingVersion]);

  const viewPrompt = viewingVersion?.snapshot?.prompt ?? skill?.prompt ?? '';
  const viewDescription = viewingVersion?.snapshot?.description ?? skill?.description ?? '';
  const boundTools: { tool_id: string; version: number | null }[] = viewSkill.tools || [];
  const toolUsage: Record<string, string> = viewSkill.tool_usage || {};
  const constraints: any = viewSkill.constraints || {};
  const knowledgeSources: any[] = Array.isArray(viewSkill.knowledge_sources) ? viewSkill.knowledge_sources : [];
  const publishedCount = versions.filter((v: any) => v.status === 'published').length;
  const sortedVersions = useMemo(() => sortVersions(versions), [versions]);

  const copyPrompt = async () => {
    try {
      await navigator.clipboard.writeText(viewPrompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard 不可用时静默 */ }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  if (!skill) return <div className="text-center py-20 text-slate-400">未找到该 Skill</div>;

  const headerMeta = viewingVersion ? STATUS_META[viewingVersion.status] || STATUS_META.offline : null;
  const viewLabel = viewingVersion
    ? (viewingVersion.status === 'draft' ? '草稿' : `v${viewingVersion.version} ${headerMeta?.label || ''}`)
    : publishedCount > 0
      ? `已发布 ${publishedCount} 版`
      : '未发布';

  return (
    <div className={cn('max-w-[1500px] mx-auto animate-in fade-in duration-500',
      inModal ? 'space-y-4 lg:space-y-0 lg:h-full lg:flex lg:flex-col lg:gap-4' : 'space-y-6')}>
      {/* 头部(弹窗里固定在顶部,不随内容滚走) */}
      <div className={cn(inModal ? 'shrink-0 space-y-3' : 'space-y-6')}>
      {/* ── 头部 ── */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => navigate('/tools/skill')} className="rounded-full">
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-bold text-slate-900 tracking-tight">{skill.name}</h1>
              <CopyId id={skill.id} showFull className="text-[11px]" />
              <Badge className={viewingVersion
                ? headerMeta?.cls
                : publishedCount > 0
                  ? 'bg-emerald-50 text-emerald-600 border-emerald-100'
                  : 'bg-slate-100 text-slate-500 border-slate-200'}>
                {viewLabel}
              </Badge>
            </div>
            <p className="text-slate-500 text-sm mt-1">{viewDescription || '（暂无描述）'}</p>
          </div>
        </div>
        <div className="flex gap-3">
          <Button variant="outline" className="rounded-xl h-10 gap-2 font-medium" onClick={() => navigate(`/tools/skill/edit?id=${id}`)}>
            <Settings2 size={16} /> 编辑 Skill
          </Button>
        </div>
      </div>

      {/* ── 未发布提示条 ── */}
      {publishedCount === 0 && (
        <div className="flex items-center justify-between flex-wrap gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <span className="text-xs font-medium text-amber-700">
            当前无已发布版本：进入编辑页保存草稿并发布后，才可作为技能被 Agent 绑定 / 调用
          </span>
          <Button size="sm" className="h-8 gap-1.5 rounded-lg bg-amber-500 hover:bg-amber-600 text-white text-xs font-semibold" onClick={() => navigate(`/tools/skill/edit?id=${id}`)}>
            <Settings2 size={13} /> 去编辑并发布
          </Button>
        </div>
      )}

      {/* ── 正在查看的版本视角 ── */}
      {viewingVersion && (
        <div className="flex items-center gap-2 flex-wrap rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-xs text-slate-500">
          <span>正在查看</span>
          <Badge className={headerMeta?.cls}>{viewLabel}</Badge>
          <span className="text-slate-400">
            · {viewingVersion.published_by_name || viewingVersion.published_by || '系统'}
            {' '}发布于 {viewingVersion.created_at ? new Date(viewingVersion.created_at).toLocaleString() : '—'}
            {' '}· 切换左侧版本可浏览历史快照
          </span>
        </div>
      )}

      </div>

      <div className={cn('grid grid-cols-1 lg:grid-cols-12 gap-6',
        inModal ? 'lg:flex-1 lg:min-h-0 lg:grid-rows-[minmax(0,1fr)]' : 'items-start')}>
        {/* 左侧版本列表 */}
        <div className={cn('lg:col-span-3 space-y-6', inModal && 'lg:h-full lg:min-h-0')}>
          <Card className={cn('border-slate-200 shadow-sm overflow-hidden',
            inModal ? 'lg:h-full lg:flex lg:flex-col' : 'lg:sticky lg:top-4')}>
            <CardHeader className="border-b bg-slate-50/50 py-3.5 px-4">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm font-bold flex items-center gap-2">
                  <Layers size={15} className="text-blue-500" /> 版本列表
                </CardTitle>
                {publishedCount > 0 && (
                  <Badge className="rounded-md bg-emerald-50 text-emerald-600 border-emerald-100 text-[10px]">已发布 {publishedCount} 版</Badge>
                )}
              </div>
            </CardHeader>
            <CardContent className={cn('p-0', inModal && 'lg:flex-1 lg:min-h-0 lg:flex lg:flex-col')}>
              <p className="px-4 py-2.5 text-[11px] leading-relaxed text-slate-400 border-b border-slate-50">
                默认展示第一个（最新）版本，点击任意版本查看其固化快照
              </p>
              <ul className={cn('overflow-y-auto divide-y divide-slate-50',
                inModal ? 'max-h-80 lg:max-h-none lg:flex-1 lg:min-h-0' : 'max-h-[calc(100vh-340px)]')}>
                {sortedVersions.map((v: any) => {
                  const meta = STATUS_META[v.status] || STATUS_META.offline;
                  const active = v.id === viewingVersion?.id;
                  return (
                    <li key={v.id} onClick={() => setSelectedVersionId(v.id)}
                      className={cn('px-4 py-3 cursor-pointer transition-colors border-l-2',
                        active ? 'bg-blue-50/70 border-blue-500' : 'hover:bg-slate-50/70 border-transparent')}>
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <Badge className={cn('rounded-md', v.status === 'published' ? 'bg-blue-50 text-blue-600 border-blue-100' : meta.cls)}>
                            {v.status === 'draft' ? '草稿' : `v${v.version}`}
                          </Badge>
                          <Badge variant="outline" className={cn('rounded-md text-[10px]', meta.cls)}>{meta.label}</Badge>
                        </div>
                        <ChevronRight size={13} className={cn('text-slate-300 shrink-0 transition-transform', active && 'text-blue-500 rotate-90')} />
                      </div>
                      <p className="mt-1.5 text-[11px] text-slate-400 truncate">
                        {v.published_by_name || v.published_by || '系统'}
                        {v.created_at ? ` · ${new Date(v.created_at).toLocaleString()}` : ''}
                      </p>
                      {v.note && <p className="mt-0.5 text-[10px] text-slate-300 truncate">{v.note}</p>}
                    </li>
                  );
                })}
                {sortedVersions.length === 0 && (
                  <li className="px-4 py-10 text-center text-xs text-slate-300">暂无版本记录：保存草稿并发布后，历史版本将展示于此</li>
                )}
              </ul>
            </CardContent>
          </Card>
        </div>

        {/* 右侧详情 */}
        <div className={cn('lg:col-span-9 grid grid-cols-1 lg:grid-cols-12 gap-6',
          inModal && 'lg:h-full lg:min-h-0 lg:grid-rows-[minmax(0,1fr)]')}>
          <div className={cn('lg:col-span-8 space-y-6', inModal && 'lg:h-full lg:overflow-y-auto custom-scrollbar lg:pr-1')}>
            {/* 系统提示词(Prompt) */}
            <Card className="border-slate-200 shadow-sm">
              <CardHeader className="border-b bg-slate-50/50 py-4">
                <div className="flex items-center justify-between gap-2">
                  <CardTitle className="text-sm font-bold flex items-center gap-2">
                    <Braces size={16} className="text-indigo-500" /> 系统提示词（Prompt）
                  </CardTitle>
                  {viewPrompt && (
                    <Button variant="ghost" size="sm" className="h-7 gap-1.5 rounded-lg text-xs text-slate-500" onClick={copyPrompt}>
                      {copied ? <Check size={13} className="text-emerald-500" /> : <Copy size={13} />}
                      {copied ? '已复制' : '复制'}
                    </Button>
                  )}
                </div>
              </CardHeader>
              <CardContent className="p-6">
                {viewPrompt ? (
                  <pre className="max-h-[480px] overflow-y-auto whitespace-pre-wrap break-words rounded-xl bg-slate-50 p-4 font-mono text-xs leading-relaxed text-slate-700">
                    {viewPrompt}
                  </pre>
                ) : (
                  <p className="text-center text-xs text-slate-300 py-6">该版本未固化提示词内容</p>
                )}
              </CardContent>
            </Card>

            {/* 工具绑定 */}
            <Card className="border-slate-200 shadow-sm">
              <CardHeader className="border-b bg-slate-50/50 py-4">
                <div className="flex items-center justify-between">
                  <CardTitle className="text-sm font-bold flex items-center gap-2">
                    <Wrench size={16} className="text-sky-500" /> 工具绑定
                  </CardTitle>
                  <Badge variant="outline" className="rounded-md text-[10px]">{boundTools.length} 个</Badge>
                </div>
              </CardHeader>
              <CardContent className="p-6">
                {boundTools.length === 0 ? (
                  <p className="text-center text-xs text-slate-300 py-4">该版本未绑定工具</p>
                ) : (
                  <div className="space-y-3">
                    {boundTools.map((pin) => {
                      const t = toolMap[pin.tool_id];
                      const usage = toolUsage[pin.tool_id];
                      return (
                        <div key={pin.tool_id} className="rounded-xl border border-slate-100 bg-slate-50/50 p-3.5">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-sm font-bold text-slate-800">{t?.name || pin.tool_id}</span>
                            <Badge variant="outline" className="rounded-md text-[10px] text-slate-500">{t?.type || '—'}</Badge>
                            {pin.version != null ? (
                              <Badge className="rounded-md bg-indigo-50 text-indigo-600 border-indigo-100 text-[10px]">锁定 v{pin.version}</Badge>
                            ) : (
                              <Badge className="rounded-md bg-emerald-50 text-emerald-600 border-emerald-100 text-[10px]">跟随最新</Badge>
                            )}
                          </div>
                          {usage && <p className="mt-1.5 text-xs text-slate-500 leading-relaxed">用法：{usage}</p>}
                        </div>
                      );
                    })}
                  </div>
                )}
              </CardContent>
            </Card>

            {/* 护栏约束 */}
            <Card className="border-slate-200 shadow-sm">
              <CardHeader className="border-b bg-slate-50/50 py-4">
                <CardTitle className="text-sm font-bold flex items-center gap-2">
                  <ShieldCheck size={16} className="text-emerald-500" /> 护栏约束
                </CardTitle>
              </CardHeader>
              <CardContent className="p-6">
                <div className="grid grid-cols-2 gap-3">
                  <div className="rounded-xl border border-slate-100 p-4">
                    <div className="text-[10px] font-bold text-slate-400 uppercase">单轮最大执行步骤</div>
                    <div className="mt-1 text-2xl font-black text-slate-800">{constraints.max_steps ?? 25}</div>
                    <div className="text-[11px] text-slate-400 mt-0.5">Max Steps</div>
                  </div>
                  <div className="rounded-xl border border-slate-100 p-4">
                    <div className="text-[10px] font-bold text-slate-400 uppercase">单轮最大工具调用</div>
                    <div className="mt-1 text-2xl font-black text-slate-800">{constraints.max_tool_calls ?? 10}</div>
                    <div className="text-[11px] text-slate-400 mt-0.5">Max Tool Calls</div>
                  </div>
                  {/* Token 预算:留空/0 = 不限制(默认)。不限制时写「不限制」而不是画一个
                      像上限的数字 —— 之前执行详情里那条 8,000 的假进度条就是这么来的。 */}
                  <div className="rounded-xl border border-slate-100 p-4">
                    <div className="text-[10px] font-bold text-slate-400 uppercase">单轮 Token 预算</div>
                    <div className="mt-1 text-2xl font-black text-slate-800">
                      {Number(constraints.token_budget) > 0
                        ? Number(constraints.token_budget).toLocaleString()
                        : <span className="text-slate-400">不限制</span>}
                    </div>
                    <div className="text-[11px] text-slate-400 mt-0.5">Token Budget · 消耗始终记录</div>
                  </div>
                  <div className="rounded-xl border border-slate-100 p-4">
                    <div className="text-[10px] font-bold text-slate-400 uppercase">严格取数</div>
                    <div className="mt-1 text-2xl font-black text-slate-800">
                      {constraints.strict_tool_execution
                        ? '已开启' : <span className="text-slate-400">关闭</span>}
                    </div>
                    <div className="text-[11px] text-slate-400 mt-0.5">Strict Tool Execution</div>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* 知识库来源 */}
            {knowledgeSources.length > 0 && (
              <Card className="border-slate-200 shadow-sm">
                <CardHeader className="border-b bg-slate-50/50 py-4">
                  <CardTitle className="text-sm font-bold flex items-center gap-2">
                    <BookOpen size={16} className="text-amber-500" /> 知识库来源
                  </CardTitle>
                </CardHeader>
                <CardContent className="p-6">
                  <div className="flex flex-wrap gap-2">
                    {knowledgeSources.map((src: any, i: number) => (
                      <Badge key={i} variant="outline" className="rounded-lg px-2.5 py-1 text-[11px] text-slate-600">
                        {typeof src === 'string' ? src : src?.name || src?.file_name || JSON.stringify(src)}
                      </Badge>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}
          </div>

          {/* 右侧信息栏 */}
          <div className={cn('lg:col-span-4 space-y-6', inModal && 'lg:h-full lg:overflow-y-auto custom-scrollbar lg:pr-1')}>
            <Card className="border-slate-200 shadow-sm overflow-hidden">
              <CardHeader className="border-b bg-slate-50/50 py-4"><CardTitle className="text-sm font-bold">基础信息</CardTitle></CardHeader>
              <CardContent className="p-5 space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-slate-500">可见性</span>
                  <Badge variant="outline" className="rounded-md">{VIS_LABEL[skill.visibility] || skill.visibility}</Badge>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-xs text-slate-500">建议模式</span>
                  <Badge className="rounded-md bg-slate-900 text-white border-none uppercase">{skill.suggested_mode || 'solo'}</Badge>
                </div>
                {skill.created_at && (
                  <div className="flex items-center gap-3 pt-1 border-t border-slate-100">
                    <div className="h-8 w-8 rounded-lg bg-slate-100 flex items-center justify-center shrink-0"><Clock size={14} className="text-slate-500" /></div>
                    <div>
                      <div className="text-[10px] text-slate-400 font-bold uppercase">创建时间</div>
                      <div className="text-xs font-bold text-slate-700">{new Date(skill.created_at).toLocaleString()}</div>
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
};

export default SkillDetail;
