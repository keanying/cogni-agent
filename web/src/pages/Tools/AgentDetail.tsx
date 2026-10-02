
import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowLeft, Settings2, Bot, MessageSquare, ShieldCheck, Rocket, Sparkles, CheckCircle2,
  AlertCircle, Layers, ChevronRight, ArrowRight, Loader2, Power, User, Zap,
  Pencil, BrainCircuit, MessageSquareText, Plus, X, RefreshCw, FilePlus2, History, Check,
} from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { Avatar, AvatarFallback, AvatarImage } from '../../components/ui/avatar';
import { Input } from '../../components/ui/input';
import { Modal } from '../../components/ui/modal';
import { useLocation, useSearchParams } from 'react-router-dom';
import { useModalNavigate } from '../../lib/modalNav';
import { useInRouteModal } from '../../components/common/RouteModal';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import VersionHistoryPanel from '../../components/common/VersionHistoryPanel';
import { Share2 } from 'lucide-react';
import { CopyId } from '../../components/ui/PageHelpers';
import { cn } from '../../lib/utils';
import { confirmDialog } from '../../components/ui/confirm';

const TONES = ['正式专业', '亲切友好', '简洁高效', '幽默风趣'];
const SCOPES = ['public', 'group', 'scenic', 'private'];
const INTENT_MODE_LABEL: Record<string, string> = {
  always: '全开（所有请求都做意图识别）',
  side_effects: '仅存在写操作/高风险工具时',
  off: '关闭（固定执行链）',
};
const VERSION_STATUS_STYLE: Record<api.VersionStatus, string> = {
  published: 'bg-emerald-50 text-emerald-600 border-emerald-200',
  draft: 'bg-amber-50 text-amber-600 border-amber-200',
  offline: 'bg-slate-100 text-slate-400 border-slate-200',
};
const VERSION_STATUS_LABEL: Record<api.VersionStatus, string> = {
  published: '已发布',
  draft: '草稿',
  offline: '已下线',
};

/** 主表字段 → 编辑快照键的映射（无草稿时的回退值）
 *  v1.1.6:description 已版本化 —— 主表值仅作「无任何版本记录」时的回退初值,
 *  存量未建版本的 agent 首次发布(基于主表派生草稿)时随之进入 v1 快照。 */
const agentToSnapshot = (a: any) => ({
  description: a.description ?? '',
  persona_desc: a.persona_desc ?? '',
  tone_style: a.tone_style ?? TONES[0],
  opening_line: a.opening_line ?? '',
  visibility: a.visibility ?? 'private',
  rate_limit_qps: a.rate_limit_qps ?? 10,
  rate_limit_daily: a.rate_limit_daily ?? 5000,
  intent_config: a.intent_config ?? null,
  // v1.2(19 号 D4-①):协作模式,主表值作无版本记录时的回退
  composition_mode: a.composition_mode ?? 'chain',
});

const AgentDetail: React.FC = () => {
  const navigate = useModalNavigate(); // 在弹窗里打开时,跳详情/编辑留在弹窗、跳回列表即关闭
  // 弹窗里:顶部固定,左侧版本列表 / 中间详情 / 右侧信息栏各自固定高度、各自滚动
  const inModal = useInRouteModal();
  const location = useLocation();
  const [params] = useSearchParams();
  const id = params.get('id');
  const versionParam = params.get('version_id');
  const [agent, setAgent] = useState<any>(null);
  const [allSkills, setAllSkills] = useState<any[]>([]);
  const [evalBatches, setEvalBatches] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  // ── 版本优先视角：多版本先展示版本列表，点某版本进入版本详情 ──
  const [versions, setVersions] = useState<api.ResourceVersion[]>([]);
  /** 版本响应全量(含 available),供 VersionHistoryPanel 复用避免重复请求 */
  const [versionInfo, setVersionInfo] = useState<{ available: boolean; unavailable_reason: string } | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [saving, setSaving] = useState(false);

  // 弹窗状态
  const [personaOpen, setPersonaOpen] = useState(false);
  const [personality, setPersonality] = useState('');
  const [tone, setTone] = useState(TONES[0]);
  const [opening, setOpening] = useState('');
  const [deployOpen, setDeployOpen] = useState(false);
  const [qps, setQps] = useState(10);
  const [daily, setDaily] = useState(5000);
  const [scope, setScope] = useState('private');

  const load = async () => {
    if (!id) { setLoading(false); return; }
    setLoading(true);
    try {
      const a = await api.getAgent(id);
      setAgent(a);
      const sr = await api.listSkills();
      setAllSkills(sr.items);
      const batches = await api.listEvalRunBatches('agent', id);
      setEvalBatches(batches.items);
      const vr = await api.listResourceVersions('agent', id);
      setVersions(vr.items);
      setVersionInfo({ available: vr.available, unavailable_reason: vr.unavailable_reason });
      // 版本选择：URL 指定 version_id 优先 → 已选中版本保留 → 默认选中列表第一个
      setSelectedVersionId((cur) => {
        if (cur && vr.items.some((v) => v.id === cur)) return cur;
        if (versionParam && vr.items.some((v) => v.id === versionParam)) return versionParam;
        const first = [...vr.items].sort((a, b) => (b.version ?? -1) - (a.version ?? -1))[0];
        return first?.id ?? null;
      });
    } catch (e: any) {
      toast.error(e.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, [id]);

  // ── 当前查看的版本 ──
  // 多版本时由 selectedVersionId 决定；单版本直接取唯一版本；无版本记录则回退主表
  const viewingVersion = useMemo(() => {
    if (selectedVersionId) return versions.find((v) => v.id === selectedVersionId) ?? null;
    if (versions.length === 1) return versions[0];
    return null;
  }, [versions, selectedVersionId]);

  // 当前视图快照：当前版本快照优先，回退主表
  const viewSnapshot = useMemo(() => ({
    ...agentToSnapshot(agent ?? {}),
    ...(viewingVersion?.snapshot || {}),
  }), [agent, viewingVersion]);

  // 当前查看版本的绑定技能：快照 bound_skills 优先（发布时冻结），否则主表
  const boundSkills = useMemo(() => {
    if (!agent) return [];
    const bound = ((viewSnapshot as any).bound_skills || []) as { skill_id: string; version: number | null }[];
    const ids = bound.length ? bound.map((b) => b.skill_id) : (agent.bound_skill_ids || []);
    return allSkills.filter((s: any) => ids.includes(s.id));
  }, [agent, allSkills, viewSnapshot]);

  // 版本态判定一律按版本表（viewingVersion）；无版本记录 = 未发布空态
  // （新建 Agent 未发布 / 迁移遗漏），视图取主表当前内容、可派生草稿后首次发布。
  // 不再读主表 status 推断发布态（Phase 1 后主表 status 非发布语义来源）。
  const isPublishedView = viewingVersion?.status === 'published';
  const isDraftView =
    viewingVersion?.status === 'draft' ||
    (viewingVersion === null && versions.length === 0);
  const isOfflineView = viewingVersion?.status === 'offline';
  // 可编辑：草稿版本或未发布空态（视图来自主表草稿内容）
  const editable = isDraftView;

  const backToAgent = () => {
    // 哪里进的回哪里：分发中心进入 → 回分发中心；有历史 → 回上一页；直接打开 URL → 回落 Agent 列表
    if (location.state?.from === 'distribution') navigate('/tools/agent/distribution');
    else if (window.history.length > 1) navigate(-1);
    else navigate('/tools/agent');
  };

  /** 详情页区块弹窗保存：只写草稿（有则更新、无则派生），不碰已发布/主表 */
  const savePartial = async (patch: Record<string, any>, label: string) => {
    if (!editable) { toast.error('当前版本已冻结，不可直接修改'); return; }
    setSaving(true);
    try {
      const merged = { ...viewSnapshot, ...patch };
      if (viewingVersion?.status === 'draft') {
        await api.editResourceVersion(viewingVersion.id, { snapshot: merged });
      } else {
        const r = await api.createResourceVersion('agent', id!, { snapshot: merged, note: '详情页快速编辑' });
        setSelectedVersionId(r.id);
      }
      await load();
      setHistoryRefresh((t) => t + 1);
      toast.success(`${label}已保存到编辑中草稿（未发布，线上不受影响），发布后生效`);
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const openPersona = () => {
    setPersonality(viewSnapshot.persona_desc ?? '');
    setTone(viewSnapshot.tone_style ?? TONES[0]);
    setOpening(viewSnapshot.opening_line ?? '');
    setPersonaOpen(true);
  };
  const openDeploy = () => {
    setQps(viewSnapshot.rate_limit_qps ?? 10);
    setDaily(viewSnapshot.rate_limit_daily ?? 5000);
    setScope(viewSnapshot.visibility ?? 'private');
    setDeployOpen(true);
  };

  /** 发布：草稿版本走版本化发布（预检+冻结快照）；存量主表草稿态 = 基于主表派生首个草稿并发布（G3 收敛统一走 /versions/{id}/publish） */
  const handlePublish = async () => {
    try {
      if (viewingVersion?.status === 'draft') {
        const r: any = await api.publishResourceVersion(viewingVersion.id);
        if (r.ok === false) { toast.error(r.message || r.hint || '发布被阻断'); return; }
        toast.success(r.message || '发布成功');
      } else {
        // 存量主表草稿态(无版本记录):G3 收敛后旧 /agents/{id}/publish 已删,
        // 首次发布 = 基于主表派生草稿并发布(评测门禁在版本发布统一路径执行)。
        const d: any = await api.createResourceVersion('agent', id!, { snapshot: agentToSnapshot(agent), note: '基于主表内容首次发布' });
        const rid = d?.id;
        if (!rid) throw new Error('草稿创建失败,请刷新后重试');
        const r: any = await api.publishResourceVersion(rid);
        if (r.ok === false) { toast.error(r.message || r.hint || '发布被阻断'); return; }
        toast.success(r.message || '发布成功');
      }
      await load();
      setHistoryRefresh((t) => t + 1);
    } catch (e: any) { toast.error(e.message || '发布失败'); }
  };

  /** 基于某版本/当前主表派生新草稿（已发布/已下线版本专用入口） */
  const deriveFromVersion = async (v?: api.ResourceVersion | null) => {
    try {
      const existingDraft = versions.find((d) => d.status === 'draft');
      if (existingDraft && !await confirmDialog({
        title: '创建新草稿',
        message: `已有编辑中的草稿${existingDraft.version ? `（v${existingDraft.version}）` : ''},新建会先放弃它,`
          + '草稿里未发布的改动将丢失。',
        confirmLabel: '放弃草稿并新建', danger: true })) return;
      if (existingDraft) await api.discardResourceVersion(existingDraft.id);
      if (v) {
        await api.createResourceVersion('agent', id!, { base_version_id: v.id, note: `基于 v${v.version} 创建草稿` });
        toast.success(`已基于 v${v.version} 创建草稿，可进入编辑页修改后发布`);
      } else {
        // 存量数据：主表已发布但无版本记录，基于当前主表快照派生
        await api.createResourceVersion('agent', id!, { snapshot: agentToSnapshot(agent), note: '基于当前已发布内容创建草稿' });
        toast.success('已基于当前已发布内容创建草稿，可进入编辑页修改后发布');
      }
      await load();
      setHistoryRefresh((t) => t + 1);
      // 直接进入编辑页编辑新草稿
      navigate(`/tools/agent/edit?id=${id}`);
    } catch (e: any) {
      toast.error(e.message || '创建草稿失败');
    }
  };

  const handleToggle = async () => {
    try { await api.toggleAgent(id!); toast.success('状态已更新'); load(); }
    catch (e: any) { toast.error(e.message || '操作失败'); }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  if (!agent) return <div className="text-center py-20 text-slate-400">未找到该 Agent</div>;

  const latestBatch = evalBatches[0];
  const intentCfg = viewSnapshot?.intent_config || null;
  const editBtn = (onClick: () => void, title = '编辑') => (
    <Button size="sm" variant="outline" className="h-8 gap-1.5 text-xs text-blue-600 border-slate-200 hover:bg-blue-50" onClick={onClick}>
      <Pencil size={12} /> {title}
    </Button>
  );
  const modalFooter = (onSave: () => void, label: string) => (
    <>
      <Button variant="outline" className="rounded-xl h-10 px-5 font-bold border-slate-200 text-slate-600" onClick={() => { setPersonaOpen(false); setDeployOpen(false); }}>取消</Button>
      <Button className="bg-blue-600 hover:bg-blue-700 text-white rounded-xl h-10 px-6 font-black gap-2" disabled={saving} onClick={onSave}>
        {saving && <Loader2 size={14} className="animate-spin" />} {label}
      </Button>
    </>
  );
  const savePersona = () => savePartial({ persona_desc: personality.trim(), tone_style: tone, opening_line: opening.trim() }, '人格设定');
  const saveDeploy = () => savePartial({ rate_limit_qps: Number(qps) || 0, rate_limit_daily: Number(daily) || 0, visibility: scope }, '部署与安全配置');

  const versionListSorted = [...versions].sort((a, b) => (b.version ?? -1) - (a.version ?? -1));

  // ── 左侧版本栏（版本列表，点击切换右侧详情；默认选中第一个） ──
  const versionSidebar = (
    <Card className={cn('border-slate-200 shadow-sm overflow-hidden',
      inModal ? 'lg:h-full lg:flex lg:flex-col' : 'lg:sticky lg:top-20')}>
      <CardHeader className="border-b bg-slate-50/50 py-3 px-4">
        <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600">
          <Layers size={15} className="text-indigo-600" /> 版本列表
          <span className="ml-auto text-[10px] text-slate-400 font-bold">共 {versions.length} 个</span>
        </CardTitle>
      </CardHeader>
      <CardContent className={cn('p-2.5 bg-slate-50/30 space-y-2',
        inModal && 'max-h-80 overflow-y-auto lg:max-h-none lg:flex-1 lg:min-h-0 custom-scrollbar')}>
        {versionListSorted.map((v) => {
          const time = v.published_at || v.created_at;
          const active = selectedVersionId === v.id;
          return (
            <button key={v.id} onClick={() => setSelectedVersionId(v.id)}
              className={cn(
                'w-full text-left rounded-xl border bg-white transition-all cursor-pointer px-3 py-2.5',
                active ? 'border-blue-400 ring-2 ring-blue-100 shadow-sm' : 'border-slate-200 hover:border-blue-300 hover:shadow-sm'
              )}>
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span className={cn('text-[13px] font-black', active ? 'text-blue-700' : 'text-slate-800')}>
                    {v.status === 'draft' ? '草稿' : `v${v.version}`}
                  </span>
                  <Badge className={`border text-[9px] font-black ${VERSION_STATUS_STYLE[v.status]}`}>{VERSION_STATUS_LABEL[v.status]}</Badge>
                </div>
                {!!v.assigned_count && (
                  <Badge className="bg-indigo-50 text-indigo-600 border-none text-[9px] font-black shrink-0">
                    {v.assigned_count} 人
                  </Badge>
                )}
              </div>
              <div className="flex items-center gap-2 mt-1.5 text-[10px] text-slate-400 font-medium">
                <span>{time ? new Date(time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}</span>
                {v.status === 'draft' && <span className="text-amber-500">· 编辑中</span>}
              </div>
              {v.note && <div className="mt-1 text-[10px] text-slate-400 font-medium truncate">{v.note}</div>}
            </button>
          );
        })}
        {versions.length === 0 && (
          <div className="text-center text-[11px] text-slate-400 py-5">暂无版本记录，保存并发布后生成</div>
        )}
        <div className="pt-2">
          <Button variant="outline" className="w-full h-9 rounded-xl gap-1.5 font-bold border-slate-200 text-xs" onClick={() => navigate(`/tools/agent/distribution?id=${id}`)}>
            <Share2 size={13} /> 前往分发中心
          </Button>
        </div>
      </CardContent>
    </Card>
  );

  // ── 详情视图（左侧版本栏 + 右侧详情，默认选中第一个版本） ──
  return (
    <div className={cn('max-w-[1500px] mx-auto animate-in fade-in duration-500',
      inModal ? 'space-y-4 lg:space-y-0 lg:h-full lg:flex lg:flex-col lg:gap-4' : 'space-y-6')}>
      {/* 头部(弹窗里固定在顶部,不随内容滚走) */}
      <div className={cn(inModal ? 'shrink-0 space-y-3' : 'space-y-6')}>
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-5">
          <Button variant="ghost" size="icon" onClick={backToAgent} className="rounded-full" title="返回 Agent 列表">
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <div className="flex items-center gap-4">
            <Avatar className="h-16 w-16 border-4 border-white shadow-xl rounded-2xl">
              {agent.avatar_url && <AvatarImage src={agent.avatar_url} />}
              <AvatarFallback className="bg-blue-600 text-white font-bold">{agent.name[0]}</AvatarFallback>
            </Avatar>
            <div>
              <div className="flex items-center gap-3">
                <h1 className="text-2xl font-black text-slate-900 tracking-tight">{agent.name}</h1>
                <CopyId id={agent.id} showFull className="text-[11px]" />
                <Badge className={agent.active ? 'bg-emerald-500 text-white border-none font-black text-[10px]' : 'bg-slate-300 text-white border-none font-black text-[10px]'}>{agent.active ? 'ONLINE（在线）' : 'OFFLINE（离线）'}</Badge>
                {/* 版本状态徽章：已发布 / 草稿 / 已下线 */}
                {isPublishedView && (
                  <Badge className="bg-emerald-50 text-emerald-600 border-emerald-200 border font-black text-[10px] gap-1">
                    <CheckCircle2 size={10} /> {viewingVersion ? `已发布 v${viewingVersion.version}` : '已发布'}
                  </Badge>
                )}
                {isDraftView && (
                  <Badge className="bg-amber-50 text-amber-600 border-amber-200 border font-black text-[10px]">
                    {viewingVersion ? '草稿（未发布）' : '未发布'}
                  </Badge>
                )}
                {isOfflineView && (
                  <Badge className="bg-slate-100 text-slate-500 border-slate-200 border font-black text-[10px]">已下线 v{viewingVersion?.version}</Badge>
                )}
              </div>
              {/* v1.1.6:description 随选中版本展示(快照优先,无版本记录回退主表初值) */}
              <p className="text-slate-500 text-sm font-medium">{viewSnapshot?.description || agent.description}</p>
            </div>
          </div>
        </div>
        <div className="flex gap-3">
          {isDraftView ? (
            <>
              <Button variant="outline" className="rounded-xl h-11 px-6 gap-2 font-bold border-slate-200"
                onClick={() => navigate(`/tools/agent/edit?id=${id}`)}>
                <Settings2 size={18} /> 编辑 Agent
              </Button>
              <Button className="bg-blue-600 hover:bg-blue-700 text-white rounded-xl h-11 px-8 font-black shadow-lg shadow-blue-100 gap-2" onClick={handlePublish}>
                <Rocket size={18} /> 发布
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" className="rounded-xl h-11 px-6 gap-2 font-bold border-slate-200 opacity-60 cursor-not-allowed"
                disabled title="该版本已冻结，不可直接修改。如需调整：基于此版本创建草稿 → 修改 → 发布为下一版本 → 重新指派">
                <Settings2 size={18} /> 编辑 Agent
              </Button>
              <Button className="bg-blue-600 hover:bg-blue-700 text-white rounded-xl h-11 px-8 font-black shadow-lg shadow-blue-100 gap-2"
                onClick={() => deriveFromVersion(viewingVersion)}>
                <FilePlus2 size={18} /> 基于此版本创建草稿
              </Button>
            </>
          )}
        </div>
      </div>

      {/* 版本状态提示条：已发布/已下线版本不可编辑 */}
      {!isDraftView && (
        <div className="flex items-start gap-3 px-4 py-3 rounded-2xl bg-amber-50 border border-amber-200 text-amber-700 text-xs font-medium animate-in fade-in duration-300">
          <AlertCircle size={15} className="shrink-0 mt-0.5" />
          <span>
            {isPublishedView
              ? <>该版本（v{viewingVersion?.version}）已<b>发布冻结</b>，生产已生效，不能直接修改。如需调整请走<b>显式迁移</b>：① 点右上角「基于此版本创建草稿」→ ② 修改并保存 → ③ 发布为下一版本 → ④ 在<b>分发中心</b>把使用方迁移到新版本（旧版本保留可回滚）。</>
              : <>该版本（v{viewingVersion?.version}）已<b>下线</b>，不再服务新请求。如需恢复或调整：点右上角「基于此版本创建草稿」→ 修改 → 重新发布。</>}
          </span>
        </div>
      )}

      </div>

      <div className={cn('grid grid-cols-1 lg:grid-cols-12 gap-6',
        inModal ? 'lg:flex-1 lg:min-h-0 lg:grid-rows-[minmax(0,1fr)]' : 'items-start')}>
        {/* 左侧版本列表 */}
        <div className={cn('lg:col-span-3 space-y-6', inModal && 'lg:h-full lg:min-h-0')}>{versionSidebar}</div>

        {/* 右侧详情：选中版本的完整详情 */}
        <div className={cn('lg:col-span-9 grid grid-cols-1 lg:grid-cols-12 gap-6',
          inModal && 'lg:h-full lg:min-h-0 lg:grid-rows-[minmax(0,1fr)]')}>
          <div className={cn('lg:col-span-8 space-y-6', inModal && 'lg:h-full lg:overflow-y-auto custom-scrollbar lg:pr-1')}>
          <Card className="border-slate-200 shadow-sm">
            <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
              <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600"><Sparkles size={16} className="text-blue-600" /> 人格设定预览</CardTitle>
              {editable && editBtn(openPersona)}
            </CardHeader>
            <CardContent className="p-8 space-y-8">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-12">
                <div className="space-y-4">
                  <h5 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] flex items-center gap-2"><User size={12} /> 性格描述</h5>
                  <p className="text-sm font-medium leading-relaxed text-slate-700 bg-blue-50/50 p-4 rounded-2xl border border-blue-100 italic">{viewSnapshot?.persona_desc || '未设置'}</p>
                </div>
                <div className="space-y-4">
                  <h5 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] flex items-center gap-2"><MessageSquare size={12} /> 语气风格</h5>
                  <div className="flex flex-wrap gap-2 pt-2">
                    <Badge className="bg-slate-900 text-white border-none px-4 py-1.5 rounded-xl font-bold uppercase tracking-wider text-[10px]">{viewSnapshot?.tone_style || '未设置'}</Badge>
                  </div>
                </div>
              </div>
              <div className="space-y-4 pt-4 border-t border-slate-100">
                <h5 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] flex items-center gap-2"><Zap size={12} /> 标准开场白</h5>
                <div className="bg-slate-50 rounded-2xl p-6 border border-slate-100">
                  <p className="text-sm font-bold text-slate-800 leading-relaxed italic">{viewSnapshot?.opening_line || '未设置开场白'}</p>
                </div>
              </div>
            </CardContent>
          </Card>

          <Card className="border-slate-200 shadow-sm overflow-hidden">
            <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
              <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600"><Layers size={16} className="text-indigo-600" /> 已绑定业务技能</CardTitle>
              {editable && editBtn(() => navigate(`/tools/agent/edit?id=${id}&focus=skills`))}
            </CardHeader>
            <CardContent className="p-4 bg-slate-50/30">
              {/* v1.2(19 号 D4-①):协作模式——多技能怎么配合,取代编排中心按技能集合反查 */}
              {boundSkills.length >= 2 && (
                <div className="mb-3 flex items-center gap-2 rounded-xl border border-indigo-100 bg-indigo-50/40 px-3 py-2">
                  <span className="text-[10px] font-bold text-indigo-400 uppercase tracking-wider shrink-0">协作模式</span>
                  <span className="text-xs font-black text-indigo-600">
                    {viewSnapshot?.composition_mode === 'pool' ? '动态池'
                      : viewSnapshot?.composition_mode === 'dag' ? '依赖图' : '固定链'}
                  </span>
                  <span className="text-[10px] text-slate-400">
                    {viewSnapshot?.composition_mode === 'pool' ? '工具合并进同一 ReAct 池,模型自主选择'
                      : viewSnapshot?.composition_mode === 'dag' ? '按依赖分层推进,同层并发'
                        : '按绑定顺序依次执行'}
                  </span>
                </div>
              )}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {boundSkills.map((skill) => (
                  <Card key={skill.id} className="border-slate-200 bg-white hover:border-blue-300 transition-all shadow-sm group">
                    <CardContent className="p-4 flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <div className="h-10 w-10 rounded-xl bg-blue-50 flex items-center justify-center text-blue-600 font-black text-xs">{skill.suggested_mode}</div>
                        <div><div className="text-sm font-black text-slate-800">{skill.name}</div><div className="text-[10px] text-slate-400 font-medium">Tools: {skill.tool_ids?.length || 0}</div></div>
                      </div>
                      <Button variant="ghost" size="icon" className="h-8 w-8 rounded-lg text-slate-300 group-hover:text-blue-600" title="查看技能详情"
                        onClick={() => navigate(`/tools/skill/detail?id=${skill.id}`)}><ArrowRight size={16} /></Button>
                    </CardContent>
                  </Card>
                ))}
                {boundSkills.length === 0 && <div className="text-center text-xs text-slate-300 py-6 col-span-2">未绑定技能</div>}
              </div>
            </CardContent>
          </Card>

          <Card className="border-slate-200 shadow-sm overflow-hidden">
            <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
              <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600"><BrainCircuit size={16} className="text-violet-600" /> 意图识别</CardTitle>
              {editable && editBtn(() => navigate(`/tools/agent/edit?id=${id}&focus=intent`), '到编辑页配置')}
            </CardHeader>
            <CardContent className="p-6">
              {!intentCfg ? (
                <div className="text-center py-6">
                  <BrainCircuit size={28} className="mx-auto text-slate-200 mb-2" />
                  <p className="text-xs text-slate-400 font-medium">未单独配置，使用框架级默认意图识别</p>
                  {editable && <p className="text-[11px] text-slate-300 mt-1">配置入口在编辑页（复杂交互，保留整页编辑）</p>}
                </div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div className="p-4 rounded-2xl border border-slate-100 bg-slate-50/50 space-y-3">
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">启用</span>
                      {intentCfg.enabled === false ? (
                        <Badge variant="outline" className="text-[10px] text-slate-400">未启用</Badge>
                      ) : (
                        <Badge className="bg-emerald-50 text-emerald-600 border-none font-black text-[10px]">已启用</Badge>
                      )}
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">判定策略</span>
                      <span className="text-xs font-black text-slate-800">{INTENT_MODE_LABEL[intentCfg.mode || 'always'] || intentCfg.mode}</span>
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">独立分诊模型</span>
                      <span className="text-xs font-black text-slate-800">{intentCfg.model || '跟随框架'}</span>
                    </div>
                  </div>
                  <div className="p-4 rounded-2xl border border-slate-100 bg-slate-50/50 space-y-3">
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">反问前二次确认</span>
                      {intentCfg.recheck_before_clarify === false ? (
                        <Badge variant="outline" className="text-[10px] text-slate-400">关闭</Badge>
                      ) : (
                        <Badge className="bg-blue-50 text-blue-600 border-none font-black text-[10px]">开启</Badge>
                      )}
                    </div>
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-black text-slate-400 uppercase tracking-widest">自定义 Prompt</span>
                      {intentCfg.prompt ? (
                        <Badge className="bg-blue-50 text-blue-600 border-none font-black text-[10px]">已配置</Badge>
                      ) : (
                        <span className="text-xs font-black text-slate-800">跟随框架</span>
                      )}
                    </div>
                  </div>
                </div>
              )}
              <p className="text-[10px] text-slate-400 leading-relaxed mt-4">
                意图识别是 Agent 级配置：一个 Agent 一个意图识别，负责把请求路由/分配到本 Agent 绑定的技能。随版本快照发布/回滚，只有已发布才在生产生效。
              </p>
            </CardContent>
          </Card>

          <Card className="border-slate-200 shadow-sm overflow-hidden">
            <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
              <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600"><Share2 size={16} className="text-blue-600" /> 版本分发</CardTitle>
            </CardHeader>
            <CardContent className="p-5">
              <p className="text-xs text-slate-500 leading-relaxed mb-4">
                哪个用户使用哪个版本，已在独立的 <b>Agent 分发中心</b> 统一管理（15 号起分发粒度收敛到人：仅按用户指派，
                无默认版本、无景区回落；未指派用户调用将报 agent_not_distributed）。
              </p>
              <Button
                onClick={() => navigate(`/tools/agent/distribution?id=${id}`)}
                className="w-full h-11 rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-bold gap-2 shadow-lg shadow-blue-200"
              >
                前往分发中心管理 <ArrowRight size={16} />
              </Button>
            </CardContent>
          </Card>
          <VersionHistoryPanel resourceType="agent" resourceId={id!} refreshKey={historyRefresh} onDraftChanged={load}
            initialVersions={versions} initialAvailable={versionInfo?.available} />
        </div>

        <div className={cn('lg:col-span-4 space-y-6', inModal && 'lg:h-full lg:overflow-y-auto custom-scrollbar lg:pr-1')}>
          <Card className="border-slate-200 shadow-sm">
            <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
              <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600"><ShieldCheck size={16} className="text-emerald-600" /> 部署与安全配置</CardTitle>
              {editable && editBtn(openDeploy)}
            </CardHeader>
            <CardContent className="p-6 space-y-6">
              <div className="space-y-4">
                <div className="flex items-center justify-between p-3 rounded-xl border border-slate-100 bg-slate-50/50"><div className="text-xs font-bold text-slate-500 uppercase">QPS 限制</div><div className="text-sm font-black text-slate-900">{viewSnapshot?.rate_limit_qps}</div></div>
                <div className="flex items-center justify-between p-3 rounded-xl border border-slate-100 bg-slate-50/50"><div className="text-xs font-bold text-slate-500 uppercase">日调用上限</div><div className="text-sm font-black text-slate-900">{viewSnapshot?.rate_limit_daily}</div></div>
                <div className="flex items-center justify-between p-3 rounded-xl border border-slate-100 bg-slate-50/50"><div className="text-xs font-bold text-slate-500 uppercase">可见范围</div><Badge className="bg-slate-900 text-white border-none text-[10px] font-black tracking-widest uppercase">{viewSnapshot?.visibility}</Badge></div>
              </div>
              <div className="h-px bg-slate-100" />
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-black text-slate-700 uppercase tracking-widest">发布门禁</span>
                  {latestBatch ? (
                    <Badge className={latestBatch.pass_rate === 1 ? 'bg-emerald-50 text-emerald-600 border-none font-black text-[10px] gap-1' : 'bg-rose-50 text-rose-600 border-none font-black text-[10px] gap-1'}>
                      <CheckCircle2 size={10} /> {(latestBatch.pass_rate * 100).toFixed(0)}%
                    </Badge>
                  ) : <Badge variant="outline" className="text-[10px] text-slate-400">无评测记录</Badge>}
                </div>
                {latestBatch && (
                  <div className="p-4 rounded-xl border border-emerald-100 bg-emerald-50/30">
                    <div className="text-[10px] text-emerald-800 font-bold leading-relaxed mb-2 uppercase tracking-tight">最近一次运行 {new Date(latestBatch.created_at).toLocaleDateString()}</div>
                    <div className="flex items-center justify-between text-xs text-emerald-700 font-black">
                      <span>回归用例集: {latestBatch.passed} / {latestBatch.total}</span>
                    </div>
                  </div>
                )}
                <Button variant="ghost" className="w-full text-blue-600 font-black text-[10px] uppercase gap-1" onClick={() => navigate('/audit/evaluation')}>
                  查看完整报告集 <ChevronRight size={14} />
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card className="border-slate-200 shadow-sm overflow-hidden border-amber-100">
            <CardHeader className="border-b bg-amber-50/50 py-4 px-6"><CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-amber-600"><AlertCircle size={16} /> 状态管理</CardTitle></CardHeader>
            <CardContent className="p-6 space-y-4">
              <Button variant="outline" className="w-full h-11 border-slate-200 rounded-xl font-bold text-slate-700 hover:bg-slate-50 gap-2" onClick={handleToggle}>
                <Power size={15} /> {agent.active ? '下线（禁用）' : '重新上线（启用）'}
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>
      </div>

      {/* 人格设定弹窗 */}
      <Modal open={personaOpen} onClose={() => setPersonaOpen(false)} title="编辑人格设定" footer={modalFooter(savePersona, '保存到草稿')}>
        <div className="space-y-5">
          <div className="space-y-1.5">
            <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">性格描述</label>
            <textarea value={personality} onChange={(e) => setPersonality(e.target.value)} placeholder="如：专业严谨, 略带幽默"
              className="w-full min-h-[100px] p-4 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 transition-all resize-none text-sm bg-blue-50/20" />
          </div>
          <div className="space-y-3">
            <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">语气风格</label>
            <div className="flex flex-wrap gap-2">
              {TONES.map((t) => (
                <button key={t} onClick={() => setTone(t)}
                  className={cn('px-4 py-2 rounded-xl border text-sm font-bold transition-all', tone === t ? 'bg-blue-600 text-white border-blue-600 shadow-md shadow-blue-100' : 'bg-white text-slate-600 border-slate-100 hover:border-slate-200')}>
                  {t}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">开场白</label>
            <textarea value={opening} onChange={(e) => setOpening(e.target.value)} placeholder="用户进入对话时看到的第一句话"
              className="w-full min-h-[80px] p-4 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 transition-all resize-none text-sm" />
          </div>
          <p className="text-[11px] text-amber-600 font-medium leading-relaxed">
            保存会写入编辑中草稿（未发布），线上仍使用已发布版本；确认后需在顶部点击「发布」才会生效。
          </p>
        </div>
      </Modal>

      {/* 部署与安全弹窗 */}
      <Modal open={deployOpen} onClose={() => setDeployOpen(false)} title="编辑部署与安全配置" footer={modalFooter(saveDeploy, '保存到草稿')}>
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">QPS 限制</label>
              <Input type="number" min={0} value={qps} onChange={(e) => setQps(Number(e.target.value))} className="h-10 rounded-xl" />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">日调用上限</label>
              <Input type="number" min={0} value={daily} onChange={(e) => setDaily(Number(e.target.value))} className="h-10 rounded-xl" />
            </div>
          </div>
          <div className="space-y-1.5">
            <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">可见范围</label>
            <div className="grid grid-cols-2 gap-2 pt-1">
              {SCOPES.map((s) => (
                <button key={s} onClick={() => setScope(s)}
                  className={cn('px-4 py-2.5 rounded-xl border text-sm font-bold capitalize transition-all', scope === s ? 'bg-blue-600 text-white border-blue-600 shadow-md shadow-blue-100' : 'bg-white text-slate-600 border-slate-100 hover:border-slate-200')}>
                  {s}
                </button>
              ))}
            </div>
          </div>
          <p className="text-[11px] text-amber-600 font-medium leading-relaxed">
            保存会写入编辑中草稿（未发布），线上仍使用已发布版本；确认后需在顶部点击「发布」才会生效。
          </p>
        </div>
      </Modal>
    </div>
  );
};

export default AgentDetail;
