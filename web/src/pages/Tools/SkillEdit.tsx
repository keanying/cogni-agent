
import React, { useEffect, useState } from 'react';
import {
  ArrowLeft, Settings2, History, MessageSquare, Boxes, Sliders, Search, Globe, Database,
  FileCode, Trash2, Variable, Zap, Loader2, Save, Rocket, RefreshCw,
} from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../../components/ui/tabs';
import { Input } from '../../components/ui/input';
import { useSearchParams } from 'react-router-dom';
import { useModalNavigate } from '../../lib/modalNav';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import VersionPanel from '../../components/common/VersionPanel';
import ParamRefPicker from '../../components/common/ParamRefPicker';
import { CopyId } from '../../components/ui/PageHelpers';
import { confirmDialog } from '../../components/ui/confirm';
import { fillLatestPins, latestPublished, type ToolPin } from './skillPins';

const TYPE_ICON: Record<string, any> = { http: Globe, db: Database, python: FileCode };


/** constraints.token_budget -> 输入框文案。
 *  null/undefined(没配)和 0(显式不限制)在表单上是同一个样子:空框 + 「留空 = 不限制」。
 *  两者对后端的区别只在「是否回退全局默认」,而全局默认出厂就是不限制,所以对填表的人
 *  没有可感知差异,不必在 UI 上制造第三种状态让人纠结。 */
const budgetToInput = (v: any): string =>
  v === null || v === undefined || v === '' || Number(v) <= 0 ? '' : String(v);

const SkillEdit: React.FC = () => {
  const navigate = useModalNavigate(); // 在弹窗里打开时,跳详情/编辑留在弹窗、跳回列表即关闭
  const [params] = useSearchParams();
  const id = params.get('id');
  const [activeTab, setActiveTab] = useState('prompt');
  const [loading, setLoading] = useState(true);
  const [skill, setSkill] = useState<any>(null);
  const [allTools, setAllTools] = useState<any[]>([]);
  const [prompt, setPrompt] = useState('');
  // 能力一句话概述(面向意图识别路由器的能力清单,描述驱动路由器标准元数据)。
  // 留空时运行时回退 system_prompt 前120字,存量 Skill 不受影响。
  const [description, setDescription] = useState('');
  const [selectedTools, setSelectedTools] = useState<ToolPin[]>([]);
  /** {tool_id: "什么时候用它"} —— 与工具绑定就近维护。
   *  分开写(工具在一个卡片、用法写进提示词)必然脱节:加了工具忘补说明、
   *  删了工具忘删说明,提示词里留着一个已不存在工具的用法,模型照着调只能得到"工具不存在"。 */
  const [toolUsage, setToolUsage] = useState<Record<string, string>>({});
  const [toolPinVersions, setToolPinVersions] = useState<Record<string, api.PinOptionVersion[]>>({});
  const [draftId, setDraftId] = useState<string | null>(null);
  const [versionToken, setVersionToken] = useState(0);
  const [maxSteps, setMaxSteps] = useState(25);
  const [maxToolCalls, setMaxToolCalls] = useState(10);
  const [strictTools, setStrictTools] = useState(false);
  // Token 预算:空字符串 = 不限制(不写进 constraints,交给全局默认)。
  // 用 string 而不是 number 存,是为了把「清空」和「填 0」区分开 —— 两者语义
  // 都是不限制,但 number 状态下清空会变成 NaN/0,回显时又变成一个凭空出现的 0。
  const [tokenBudget, setTokenBudget] = useState('');
  // 保留 constraints 里的其他键(如 strict_tool_execution/route_hit_keywords/model):
  // 表单只编辑 max_steps/max_tool_calls,组装时合并原值,否则保存会抹掉它们
  const [extraConstraints, setExtraConstraints] = useState<Record<string, any>>({});
  const [toolSearch, setToolSearch] = useState('');
  const [saving, setSaving] = useState(false);
  /** 最新已发布版本号(15 号无默认;徽标与版本卡一律按版本表推导,不再读主表 status) */
  const [publishedVersion, setPublishedVersion] = useState<number | null>(null);

  const load = async () => {
    if (!id) { setLoading(false); return; }
    setLoading(true);
    try {
      const [s, t, versions] = await Promise.all([
        api.getSkill(id), api.listTools(), api.listResourceVersions('skill', id),
      ]);
      setSkill(s); setAllTools(t.items);
      const published = versions.items.filter((v) => v.status === 'published');
      setPublishedVersion(published.length ? Math.max(...published.map((v) => v.version ?? 0)) : null);
      setPrompt(s.prompt || '');
      setDescription(s.description || '');
      const draft = versions.items.find((v) => v.status === 'draft');
      if (draft) {
        // 编辑区 = draft:已保存的草稿优先于主表,保证不丢未发布改动(P1-B0:prompt 同 description 进快照)
        setDraftId(draft.id);
        const snap = draft.snapshot || {} as any;
        setPrompt(snap.prompt || s.prompt || '');
        setDescription(snap.description || s.description || '');
        setToolUsage(snap.tool_usage || s.tool_usage || {});
        setMaxSteps(snap.constraints?.max_steps || s.constraints?.max_steps || 25);
        setMaxToolCalls(snap.constraints?.max_tool_calls || s.constraints?.max_tool_calls || 10);
        setStrictTools(!!(snap.constraints?.strict_tool_execution ?? s.constraints?.strict_tool_execution));
        setTokenBudget(budgetToInput(snap.constraints?.token_budget ?? s.constraints?.token_budget));
        setExtraConstraints({ ...((snap.constraints || s.constraints) || {}) });
        setSelectedTools((snap.tools || []).map((p: any) => ({ tool_id: p.tool_id, version: p.version ?? null })));
      } else {
        setToolUsage(s.tool_usage || {});
        setMaxSteps(s.constraints?.max_steps || 25);
        setMaxToolCalls(s.constraints?.max_tool_calls || 10);
        setStrictTools(!!s.constraints?.strict_tool_execution);
        setTokenBudget(budgetToInput(s.constraints?.token_budget));
        setExtraConstraints({ ...(s.constraints || {}) });
        setSelectedTools((s.tool_ids || []).map((tid: string) => ({ tool_id: tid, version: null })));
      }
      // 版本下拉候选:有 draft 时基于草稿快照的绑定,否则基于主表
      const opts = await api.getPinOptions('skill', id, draft?.id);
      const map: Record<string, api.PinOptionVersion[]> = {};
      opts.bindings.forEach((b) => { map[b.resource_id] = b.versions; });
      setToolPinVersions(map);
      // 编辑时默认最新版本:未钉版的绑定自动选中各自最新的已发布版本。
      // 以前这里留着 null,27 个工具全显示「未钉版」,要一个个下拉去选,不选就发布不了。
      setSelectedTools((prev) => {
        const { pins, filled } = fillLatestPins(prev, map);
        if (filled) toast.info(`${filled} 个工具未钉版,已默认选中最新已发布版本(保存草稿后生效)`);
        return pins;
      });
    } catch (e: any) {
      toast.error(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, [id]);

  const isSelected = (tid: string) => selectedTools.some((p) => p.tool_id === tid);

  /** 该工具当前可选的最新 published 版本(派生默认填它) */
  const latestPinVersion = (toolId: string): number | null => latestPublished(toolPinVersions[toolId]);

  /** 版本候选只在进页面时按「已绑定的工具」拉过一次;新勾上的工具没有候选,
   *  下拉是空的、也就没法默认最新版本。勾选时按需补拉。 */
  const ensurePinOptions = async (tids: string[]) => {
    const missing = tids.filter((t) => !toolPinVersions[t]);
    if (!missing.length) return toolPinVersions;
    const fetched = await Promise.all(missing.map(async (t) => {
      try {
        const r = await api.listResourceVersions('tool', t);
        const vs: api.PinOptionVersion[] = r.items
          .map((v) => ({ version: v.version, version_id: v.id, status: v.status }))
          .sort((a, b) => (b.version ?? -1) - (a.version ?? -1));
        return [t, vs] as const;
      } catch { return [t, [] as api.PinOptionVersion[]] as const; }
    }));
    const merged = { ...toolPinVersions, ...Object.fromEntries(fetched) };
    setToolPinVersions(merged);
    return merged;
  };

  /** 解绑时同步清掉使用说明。留着会变成一条指向不存在工具的描述——
   *  这正是"工具和说明分开维护"会产生的那类垃圾数据。 */
  const dropUsage = (tids: string[]) => setToolUsage((prev) => {
    if (!tids.some((t) => prev[t])) return prev;
    const next = { ...prev }; tids.forEach((t) => delete next[t]); return next;
  });

  const addTools = async (tids: string[]) => {
    const fresh = tids.filter((t) => !isSelected(t));
    if (!fresh.length) return;
    // 先放进去(未钉版),候选拉回来后补上最新版本 —— 不让勾选等网络
    setSelectedTools((prev) => [...prev, ...fresh.filter((t) => !prev.some((p) => p.tool_id === t))
      .map((t) => ({ tool_id: t, version: latestPinVersion(t) }))]);
    const opts = await ensurePinOptions(fresh);
    setSelectedTools((prev) => fillLatestPins(prev, opts).pins);
  };

  const removeTools = (tids: string[]) => {
    setSelectedTools((prev) => prev.filter((p) => !tids.includes(p.tool_id)));
    dropUsage(tids);
  };

  const toggleTool = (tid: string) => (isSelected(tid) ? removeTools([tid]) : addTools([tid]));

  /** 批量:对**当前搜索结果**全选 / 全不选。取消时如果会丢掉已填的使用说明,先确认。 */
  const toggleAllFiltered = async (ids: string[], allOn: boolean) => {
    if (!allOn) return addTools(ids);
    const withUsage = ids.filter((t) => (toolUsage[t] || '').trim());
    if (withUsage.length && !(await confirmDialog({
      title: `取消选择 ${ids.length} 个工具?`,
      message: `其中 ${withUsage.length} 个已填写「什么时候用它」,取消后这些说明会一起清掉。`,
      confirmLabel: '取消选择', danger: true,
    }))) return;
    removeTools(ids);
  };
  const setToolPin = (tid: string, version: number | null) =>
    setSelectedTools((prev) => prev.map((p) => (p.tool_id === tid ? { ...p, version } : p)));
  const handleInsert = (text: string) => setPrompt((prev) => `${prev} {{${text}}}`);

  /** 刷新到最新依赖:有草稿走后端(同时固化快照),否则本地按最新 published 填 pin */
  const refreshPins = async () => {
    try {
      if (draftId) {
        const r = await api.refreshVersionPins(draftId);
        const pins = (r.snapshot?.tools || []) as { tool_id: string; version: number | null }[];
        setSelectedTools(pins.map((p) => ({ tool_id: p.tool_id, version: p.version ?? null })));
        toast.success(r.changed?.length ? `已刷新 ${r.changed.length} 个依赖:${r.changed.join(', ')}` : '依赖均已是最新');
      } else {
        setSelectedTools((prev) => prev.map((p) => ({ ...p, version: latestPinVersion(p.tool_id) })));
        toast.success('已刷新为各依赖的最新 published 版本');
      }
    } catch (e: any) {
      toast.error(e.message || '刷新失败');
    }
  };

  /** 编辑区 snapshot:版本化字段 + 绑定 pin + prompt(随版本发布,发布时物化写回主表 tc_prompts)。
   *  收敛双轨(P1-B0):prompt 不再走 updateSkill 直写主表,统一进草稿快照。 */
  const buildSnapshot = () => ({
    name: skill?.name,
    description,
    prompt,
    tool_usage: toolUsage,
    // token_budget:留空 -> 传 null,后端当「没配」回退全局默认;填 0 -> 显式不限制。
    // 不能省略这个键,否则改「有上限」为「跟随全局」时 extraConstraints 里的旧值会被留下。
    constraints: { ...extraConstraints, max_steps: maxSteps, max_tool_calls: maxToolCalls,
                   strict_tool_execution: strictTools,
                   token_budget: tokenBudget.trim() === '' ? null : Math.max(0, Number(tokenBudget) || 0) },
    suggested_mode: skill?.suggested_mode,
    knowledge_sources: skill?.knowledge_sources,
    visibility: skill?.visibility,
    tools: selectedTools,
  });

  /** 保存草稿(P1-B0):单通道写入草稿快照(prompt/description 均随版本发布时物化),不再双写主表 */
  const saveDraft = async (showToast = true): Promise<string> => {
    if (!id) throw new Error('缺少 Skill id');
    let draft = draftId;
    if (draft) {
      await api.editResourceVersion(draft, { snapshot: buildSnapshot() });
    } else {
      const r = await api.createResourceVersion('skill', id, { snapshot: buildSnapshot(), note: '编辑区保存' });
      draft = r.id;
      setDraftId(r.id);
      setVersionToken((t) => t + 1);
    }
    if (showToast) toast.success('草稿已保存(未发布,线上不受影响)');
    return draft!;
  };

  const handleSaveDraft = async () => {
    setSaving(true);
    try {
      await saveDraft();
      load();
    } catch (e: any) {
      toast.error(e.message || '保存草稿失败');
    } finally {
      setSaving(false);
    }
  };

  /** 发布:先预检(依赖闭包校验,拦截不可用依赖),再 publish。15 号:无默认语义 */
  const handlePublish = async () => {
    if (!id) return;
    setSaving(true);
    try {
      const draft = draftId || (await saveDraft(false));
      // 推荐方案:发布预检(dry-run) —— 绑定里存在 DRAFT/已下线/不存在的工具会被拦截;version:null 模糊 pin 同样被拦
      const pc = await api.precheckResourceVersion(draft);
      if (!pc.pass) {
        const brief = pc.problems.slice(0, 3).join(';') +
          (pc.problems.length > 3 ? ` 等共 ${pc.problems.length} 项` : '');
        toast.error(`发布预检未通过:${brief}。请先处理后再发布。`, { duration: 8000 });
        setSaving(false);
        return;
      }
      const r = await api.publishResourceVersion(draft);
      toast.success(r.message || `v${r.version} 已发布`);
      if (r.hint) toast.warning(r.hint, { duration: 10000 });
      load();
    } catch (e: any) {
      toast.error(e.message || '发布失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  if (!skill) return <div className="text-center py-20 text-slate-400">未找到该 Skill</div>;

  const filteredTools = allTools.filter((t) => !toolSearch || t.name.includes(toolSearch));
  const filteredSelected = filteredTools.filter((t) => isSelected(t.id)).length;
  const allFilteredOn = filteredTools.length > 0 && filteredSelected === filteredTools.length;

  return (
    <div className="max-w-full space-y-6 animate-in fade-in duration-500">
      <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
        {/* 标题 + 操作按钮 + 页签整体吸顶:工具列表很长,往下翻时「保存/发布」和页签不能跟着滚走。
            背景色与布局底色一致,负边距盖住左右内边距,滚动内容从下面穿过时不会露出来。 */}
        <div className="sticky -top-6 z-30 -mx-6 -mt-6 px-6 pt-6 pb-3 bg-[#f8fafc] space-y-4 border-b border-transparent">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <div className="flex items-center gap-4">
              <Button variant="ghost" size="icon" onClick={() => navigate('/tools/skill')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
              <div>
                <div className="flex items-center gap-3">
                  <h1 className="text-2xl font-bold text-slate-900 tracking-tight">{skill.name}</h1>
                  <CopyId id={skill.id} showFull className="text-[11px]" />
                  <Badge className="bg-blue-50 text-blue-600 border-blue-100 uppercase font-black tracking-tighter">MODE: {skill.suggested_mode}</Badge>
                  {draftId ? (
                    <Badge className="bg-amber-50 text-amber-600 border-amber-100">草稿编辑中（未发布）</Badge>
                  ) : publishedVersion ? (
                    <Badge className="bg-emerald-50 text-emerald-600 border-emerald-100 font-mono">v{publishedVersion} 已发布</Badge>
                  ) : (
                    <Badge className="bg-amber-50 text-amber-600 border-amber-100">未发布</Badge>
                  )}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button disabled={saving} variant="outline" className="rounded-xl h-10 px-5 font-bold gap-2 border-slate-200 text-slate-600 hover:bg-slate-50" onClick={handleSaveDraft}>
                {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />} 保存草稿(不生效)
              </Button>
              <Button disabled={saving} className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl h-10 px-8 font-bold shadow-lg shadow-blue-100 gap-2" onClick={handlePublish}>
                {saving ? <Loader2 size={15} className="animate-spin" /> : <Rocket size={15} />} 发布版本
              </Button>
            </div>
          </div>

        <TabsList className="w-full justify-start h-12 bg-slate-100/50 p-1 border rounded-xl gap-1">
          <TabsTrigger value="prompt" className="flex-1 max-w-[160px] rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2"><MessageSquare size={16} /> 提示词模板</TabsTrigger>
          <TabsTrigger value="tools" className="flex-1 max-w-[160px] rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2"><Boxes size={16} /> 工具接入</TabsTrigger>
          <TabsTrigger value="constraints" className="flex-1 max-w-[160px] rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2"><Sliders size={16} /> 护栏约束</TabsTrigger>
          <TabsTrigger value="version" className="flex-1 max-w-[160px] rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2"><History size={16} /> 版本历史</TabsTrigger>
        </TabsList>
        </div>

        <div className="mt-3 min-h-[500px]">
          <TabsContent value="prompt" className="space-y-6">
            <Card className="border-slate-200 shadow-sm">
              <CardContent className="p-4 space-y-2">
                <label className="text-sm font-semibold text-slate-700">能力描述（一句话概述）</label>
                <input
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="如:舆情监控与分析,定时抓取全网舆情并生成分析报告(供意图识别路由器识别,留空自动用提示词前120字)"
                  className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none focus:border-blue-400" />
              </CardContent>
            </Card>
            <Card className="border-slate-200 shadow-sm">
              <div className="border-b px-6 py-3 bg-slate-50/80 flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <Button variant="ghost" size="sm" className="h-8 rounded-lg text-xs font-bold gap-1.5" onClick={() => handleInsert('scenic_id')}><Variable size={14} /> 插入 {'{{scenic_id}}'}</Button>
                  <Button variant="ghost" size="sm" className="h-8 rounded-lg text-xs font-bold gap-1.5" onClick={() => handleInsert('now')}><Variable size={14} /> 插入 {'{{now}}'}</Button>
                  {/* 系统参数(系统设置 · 参数配置):按当前景区取值渲染;敏感参数渲染成「已配置」 */}
                  <ParamRefPicker onPick={(ref) => setPrompt((prev) => `${prev} ${ref}`)} />
                </div>
                <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">{prompt.length} Characters</span>
              </div>
              <CardContent className="p-0">
                <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)}
                  className="w-full min-h-[400px] p-6 text-sm font-mono leading-relaxed bg-white outline-none resize-none focus:ring-0"
                  placeholder="请输入 Prompt 行为定义..." />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="tools">
            {/* 两栏固定为「一屏」高,各自内部滚动。
                以前写死 h-[600px] 但栅格子项默认 min-height:auto,内容一多就把自己撑高、
                溢出到 600px 外面 —— 下面的「版本历史」卡片按 600px 排版,于是整块压在工具列表上。
                min-h-0 让子项服从这个高度,溢出的部分交给 overflow-y-auto。 */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 lg:h-[calc(100vh-250px)] lg:min-h-[420px]">
              <div className="flex min-h-0 flex-col gap-3 max-lg:h-[70vh]">
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-bold text-slate-700 uppercase tracking-tight">可用能力池</h4>
                  <div className="flex items-center gap-2">
                    <Button size="sm" variant="outline" className="h-7 gap-1.5 text-[11px]" disabled={selectedTools.length === 0} onClick={refreshPins}>
                      <RefreshCw size={11} /> 刷新到最新依赖
                    </Button>
                    <Badge variant="outline" className="rounded-full text-[10px] font-bold">ALL: {allTools.length}</Badge>
                  </div>
                </div>
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
                  <Input value={toolSearch} onChange={(e) => setToolSearch(e.target.value)} placeholder="搜索工具..." className="pl-9 h-10 rounded-xl bg-white" />
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain border rounded-xl divide-y bg-white">
                  {/* 批量:对当前搜索结果全选 / 全不选(先搜「舆情」再全选 = 一次挂上整组) */}
                  {filteredTools.length > 0 && (
                    <label className="sticky top-0 z-10 flex cursor-pointer items-center justify-between bg-slate-50 px-3 py-2 text-[12px] font-bold text-slate-600">
                      <span>
                        {toolSearch ? `全选搜索结果(${filteredTools.length})` : `全选(${filteredTools.length})`}
                        <span className="ml-2 font-normal text-slate-400">已选 {filteredSelected} / {filteredTools.length}</span>
                      </span>
                      <input type="checkbox" className="h-4 w-4 rounded border-slate-300 text-blue-600"
                        checked={allFilteredOn}
                        ref={(el) => { if (el) el.indeterminate = filteredSelected > 0 && !allFilteredOn; }}
                        onChange={() => toggleAllFiltered(filteredTools.map((t) => t.id), allFilteredOn)} />
                    </label>
                  )}
                  {filteredTools.map((tool) => {
                    const Icon = TYPE_ICON[tool.type] || Globe;
                    return (
                      <div key={tool.id} onClick={() => toggleTool(tool.id)} className="flex items-center justify-between p-3 hover:bg-slate-50 cursor-pointer transition-colors group">
                        <div className="flex items-center gap-3">
                          <div className="h-8 w-8 rounded-lg bg-slate-100 flex items-center justify-center text-slate-500 group-hover:bg-blue-50 group-hover:text-blue-600">
                            <Icon size={14} />
                          </div>
                          <div>
                            <div className="text-sm font-bold text-slate-700">{tool.name}</div>
                            <div className="text-[10px] text-slate-400 font-mono tracking-tighter uppercase">{tool.type} · {tool.version}</div>
                          </div>
                        </div>
                        <input type="checkbox" checked={isSelected(tool.id)} readOnly className="h-4 w-4 rounded border-slate-300 text-blue-600" />
                      </div>
                    );
                  })}
                </div>
              </div>
              <div className="flex min-h-0 flex-col gap-3 max-lg:h-[70vh]">
                <div>
                  <h4 className="text-sm font-bold text-slate-700 uppercase tracking-tight">已接入本 Skill 的工具 ({selectedTools.length})</h4>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-400">
                    每个工具下方填「什么时候用它」,保存后系统会按本次实际可用的工具列表自动合成进提示词
                    —— <b>不需要手写进说明书</b>,那样加删工具时两边容易对不上。
                  </p>
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain border rounded-xl bg-slate-50/50 p-4 space-y-3">
                  {selectedTools.map(({ tool_id: tid, version }) => {
                    const tool = allTools.find((t) => t.id === tid);
                    if (!tool) return null;
                    return (
                      <Card key={tid} className="border-slate-200 shadow-sm bg-white hover:border-blue-200 transition-all">
                        <CardContent className="p-3 space-y-2">
                          <div className="flex items-center justify-between">
                            <div className="flex items-center gap-3">
                              <div className="h-8 w-8 rounded-lg bg-blue-50 flex items-center justify-center text-blue-600"><Boxes size={16} /></div>
                              <div>
                                <div className="text-sm font-bold text-slate-800">{tool.name}</div>
                                <div className="text-[10px] text-slate-500 line-clamp-1">{tool.description}</div>
                              </div>
                            </div>
                            <div className="flex items-center gap-2">
                              <select
                                value={version ?? ''}
                                onChange={(e) => setToolPin(tid, e.target.value === '' ? null : Number(e.target.value))}
                                className="h-7 rounded-lg border border-slate-200 bg-white px-1.5 text-[11px] font-bold text-slate-600 outline-none focus:border-blue-400">
                                <option value="">未钉版(发布前必须显式选择)</option>
                                {(toolPinVersions[tid] || []).map((v) => (
                                  // v1.1.5:未发布版本也可被选到(可预览),但 pin 只能钉已发布版本,
                                  // 故 draft 选项 disabled 且标注「未发布」——发布后即变为可 pin 的正式版本。
                                  <option key={v.version_id} value={v.version ?? ''} disabled={v.status !== 'published'}>
                                    {v.status === 'draft'
                                      ? '未发布（发布后可用）'
                                      : v.status === 'offline'
                                        ? `v${v.version}(已下线)`
                                        : `v${v.version}${v.version === latestPinVersion(tid) ? '(最新)' : ''}`}
                                  </option>
                                ))}
                              </select>
                              <Button variant="ghost" size="icon" className="h-7 w-7 text-rose-400 hover:text-rose-600" onClick={() => toggleTool(tid)}><Trash2 size={14} /></Button>
                            </div>
                          </div>
                          {/* 就近填写使用场景。保存时由后端按**实际解析出的工具列表**
                              自动合成进系统提示,不需要也不应该手写进提示词。 */}
                          <input
                            value={toolUsage[tid] || ''}
                            onChange={(e) => setToolUsage((prev) => ({ ...prev, [tid]: e.target.value }))}
                            placeholder="什么时候用这个工具?(留空则让模型按工具自身描述判断)"
                            className="w-full rounded-lg border border-slate-200 bg-slate-50/60 px-2.5 py-1.5 text-[12px] outline-none focus:border-blue-400 focus:bg-white" />
                        </CardContent>
                      </Card>
                    );
                  })}
                  {selectedTools.length === 0 && <div className="text-center text-xs text-slate-300 py-10">尚未绑定工具</div>}
                  <p className="text-[10px] leading-relaxed text-slate-400">
                    15 号起每个工具绑定需<b>显式钉选已发布版本</b>(version:null 模糊 pin 会被发布预检拦截);
                    工具改动随 Skill 新版本发布与依赖锁一起冻结。
                  </p>
                </div>
              </div>
            </div>
          </TabsContent>

          <TabsContent value="constraints">
            <Card className="border-slate-200 shadow-sm max-w-lg">
              <CardContent className="p-6 space-y-6">
                <div className="space-y-2">
                  <label className="text-sm font-semibold text-slate-700">最大步数（超过将熔断任务）</label>
                  <Input type="number" value={maxSteps} onChange={(e) => setMaxSteps(Number(e.target.value) || 0)} className="h-11 rounded-xl" />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-semibold text-slate-700">最大工具调用次数（超过将熔断任务）</label>
                  <Input type="number" value={maxToolCalls} onChange={(e) => setMaxToolCalls(Number(e.target.value) || 0)} className="h-11 rounded-xl" />
                </div>
                <div className="space-y-2">
                  <label className="flex cursor-pointer items-center justify-between rounded-xl border border-slate-200 p-4">
                    <span>
                      <span className="block text-sm font-semibold text-slate-700">严格取数（strict_tool_execution）</span>
                      <span className="mt-0.5 block text-xs text-slate-500">
                        本轮尚无工具调用时，协议级强制模型发起取数调用（tool_choice=required）——
                        数据查询类技能防「凭上下文直接作答」；首次调用后恢复模型自主决策
                      </span>
                    </span>
                    <input type="checkbox" checked={strictTools}
                      onChange={(e) => setStrictTools(e.target.checked)}
                      className="h-5 w-5 accent-blue-600" />
                  </label>
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-semibold text-slate-700">Token 预算（超过将熔断任务）</label>
                  <Input type="number" min={0} value={tokenBudget} placeholder="留空 = 不限制"
                         onChange={(e) => setTokenBudget(e.target.value)} className="h-11 rounded-xl" />
                  <p className="text-xs leading-relaxed text-slate-500">
                    单轮任务累计的 prompt + completion 上限。<b>留空或填 0 = 不限制</b>（默认）——
                    无论是否设限，消耗都会照常记录并计费，执行详情和成本账不受影响。
                    要卡整体花销请用<b>系统设置 → 预算护栏 → 每日 Token 上限</b>（按景区/集团，天级）。
                  </p>
                </div>
                <div className="p-4 bg-blue-50/50 border border-blue-100 rounded-xl text-xs text-blue-700 flex items-start gap-2">
                  <Zap size={14} className="mt-0.5 shrink-0" />
                  <span>
                    这些阈值会覆盖系统全局默认护栏配置，用于防止执行引擎陷入死循环或无限烧 Token。
                    一条技能链上有多个技能时，Token 预算按<b>设了上限的那几个里最严格的一个</b>生效。
                  </span>
                </div>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="version">
            <Card className="border-slate-200 shadow-sm overflow-hidden">
              <CardContent className="p-0">
                <div className="p-6">
                  <div className="flex items-center gap-3 mb-4">
                    <span className="text-sm font-bold text-slate-600 uppercase tracking-widest">最新已发布版本</span>
                    <Badge className="bg-violet-500 text-white border-none font-mono text-sm px-3 py-1">
                      {publishedVersion ? `v${publishedVersion}` : '未发布'}
                    </Badge>
                    {prompt && (
                      <span className="text-xs text-slate-400">· 提示词已配置</span>
                    )}
                  </div>
                  <p className="text-xs text-slate-500 leading-relaxed">
                    15 号起无默认版本：本 Skill 不单独对用户生效，只有被 Agent 版本快照
                    <b>钉版引用</b>后随 Agent 分发到用户（按依赖锁解析）。编辑内容一律存入草稿，
                    点击「发布版本」才生成新快照并递增版本号（v1 → v2 → v3…）；
                    发布记录与历史版本见下方版本面板。
                  </p>
                </div>
              </CardContent>
            </Card>
            {/* 版本面板只放在「版本历史」页签里。以前挂在所有页签下面,
                工具接入页因此总要整页滚动 —— 列标题、搜索框跟着滚走,还和工具列表叠在一起。 */}
            {id && (
              <Card className="mt-6 border-slate-200 shadow-sm">
                <CardContent className="p-5">
                  <VersionPanel resourceType="skill" resourceId={id} refreshToken={versionToken} onChanged={load} />
                </CardContent>
              </Card>
            )}
          </TabsContent>
        </div>
      </Tabs>

    </div>
  );
};

export default SkillEdit;
