
import React, { useEffect, useState } from 'react';
import { ArrowLeft, User, Sparkles, Zap, Rocket, Plus, Search, CheckCircle2, Cpu, Activity, Loader2, RefreshCw, Save, MessageSquareText, X, BrainCircuit, Info } from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { Avatar, AvatarFallback, AvatarImage } from '../../components/ui/avatar';
import { Badge } from '../../components/ui/badge';
import { cn } from '../../lib/utils';
import { useSearchParams } from 'react-router-dom';
import { useModalNavigate } from '../../lib/modalNav';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import VersionPanel from '../../components/common/VersionPanel';

const TONES = ['正式专业', '亲切友好', '简洁高效', '幽默风趣'];
const SCOPES = ['public', 'group', 'scenic', 'private'];

/** 技能绑定:skill_id + 版本 pin。15 号起 version=null = 尚未钉版(发布预检拦截,需显式钉已发布版本) */
interface SkillPin { skill_id: string; version: number | null; }

const AgentNew: React.FC = () => {
  const navigate = useModalNavigate(); // 在弹窗里打开时,跳详情/编辑留在弹窗、跳回列表即关闭
  const [params] = useSearchParams();
  const editId = params.get('id');
  const isEdit = !!editId;

  const [loading, setLoading] = useState(isEdit);
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [avatarUrl, setAvatarUrl] = useState('');
  const [personality, setPersonality] = useState('专业严谨, 略带幽默');
  const [tone, setTone] = useState('正式专业');
  const [openingLine, setOpeningLine] = useState('');
  const [skills, setSkills] = useState<any[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<SkillPin[]>([]);
  const [skillPinVersions, setSkillPinVersions] = useState<Record<string, api.PinOptionVersion[]>>({});
  const [draftId, setDraftId] = useState<string | null>(null);
  const [versionToken, setVersionToken] = useState(0);
  // 已发布状态:用于"冻结版本+显式迁移"提示 —— 已发布且无草稿时,本页为派生草稿模式。
  // 已发布判定只信版本表 published 版本(主表 status 已废弃),见下方 hasPublished。
  const [hasPublished, setHasPublished] = useState(false);
  const [skillSearch, setSkillSearch] = useState('');
  const [qps, setQps] = useState(10);
  const [dailyLimit, setDailyLimit] = useState(5000);
  const [scope, setScope] = useState('private');
  // v1.2(19 号 D4-①):多技能协作模式收进 agent(替代编排中心按技能集合反查)。
  // chain=按绑定顺序固定链执行(默认)/ pool=工具合并进同一 ReAct 池(动态对话)/ dag=依赖图(边按顺序派生)。
  // 仅绑定 >=2 个技能时有意义;版本化字段,随草稿保存/发布生效。
  const [compositionMode, setCompositionMode] = useState('chain');
  // v1.1.5 意图识别 agent 化:一个 Agent 一个意图识别 —— 是否开意图识别/判定策略/
  // 独立分诊模型/自定义分诊 prompt/反问前二次确认开关,全部按 Agent 单独配置
  const [intentEnabled, setIntentEnabled] = useState(true);
  const [intentMode, setIntentMode] = useState('always');
  const [intentModel, setIntentModel] = useState('');
  const [intentPrompt, setIntentPrompt] = useState('');
  const [intentRecheck, setIntentRecheck] = useState(true);
  // v1.1.7 分诊历史注入深度(0-5 轮):>0 时 Triager 判定前注入最近 N 轮对话文本,
  // 支撑跨轮话题延续(用户回答澄清追问/省略表述);0 = 不注入(单轮隔离,缺省)。
  const [intentHistoryRounds, setIntentHistoryRounds] = useState(0);
  // v1.1.5 意图分析模型可配置:下拉候选直接来自「系统设置 · 对话模型」清单
  // (/admin/settings/models,过滤 model_type=chat),value 取配置主键 id(不可变,
  // 后端 _match_db_config 按 id→name→model_id 顺序解析,id 最稳);支持自定义模型名
  const [models, setModels] = useState<any[]>([]);
  const [intentCustomModel, setIntentCustomModel] = useState(false);

  const buildIntentConfig = (): any => ({
    enabled: intentEnabled,
    mode: intentMode,
    model: intentModel.trim() || undefined,
    prompt: intentPrompt.trim() || undefined,
    recheck_before_clarify: intentRecheck,
    // v1.1.7:history_rounds 为 0(缺省=不注入)时不发字段,保持配置快照干净;
    // 后端未配置即 0,与显式 0 语义一致。
    history_rounds: intentHistoryRounds > 0 ? intentHistoryRounds : undefined,
  });

  /** 从主表 / 草稿快照回填意图识别配置(快照优先,兼容未配置过的旧数据) */
  const loadIntentConfig = (src: any) => {
    const cfg = src?.intent_config || {};
    setIntentEnabled(cfg.enabled ?? true);
    setIntentMode(cfg.mode || 'always');
    setIntentModel(cfg.model || '');
    setIntentPrompt(cfg.prompt || '');
    setIntentRecheck(cfg.recheck_before_clarify ?? true);
    setIntentHistoryRounds(Number(cfg.history_rounds) || 0);
  };

  useEffect(() => {
    api.listSkills().then((r) => setSkills(r.items)).catch(() => {});
    // 意图分析模型候选:与「系统设置 · 对话模型」清单同源(/admin/settings/models),
    // 仅取启用中的对话模型(model_type=chat),与页面模型 tab 保持一致
    api.listModelConfigs()
      .then((r) => setModels((r.items || []).filter((m: any) => m.model_type === 'chat' && m.active)))
      .catch(() => {});
    if (!editId) { setLoading(false); return; }
    (async () => {
      try {
        const [a, versions] = await Promise.all([
          api.getAgent(editId),
          api.listResourceVersions('agent', editId),
        ]);
        setHasPublished(versions.items.some((v) => v.status === 'published'));
        setName(a.name); setDescription(a.description || ''); setAvatarUrl(a.avatar_url || '');
        setQps(a.rate_limit_qps); setDailyLimit(a.rate_limit_daily); setScope(a.visibility);
        setCompositionMode(a.composition_mode || 'chain');
        loadIntentConfig(a);
        const draft = versions.items.find((v) => v.status === 'draft');
        if (draft) {
          // 编辑区 = draft:已保存的草稿优先于主表,保证不丢未发布改动
          setDraftId(draft.id);
          const s = draft.snapshot || {} as any;
          setName(s.name ?? a.name);
          // v1.1.6:description 已版本化 —— 草稿快照优先(存量草稿无该键时回退主表初值)
          setDescription(s.description ?? a.description ?? '');
          setPersonality(s.persona_desc || a.persona_desc || '');
          setTone(s.tone_style || a.tone_style || TONES[0]);
          setOpeningLine(s.opening_line || a.opening_line || '');
          setQps(s.rate_limit_qps ?? a.rate_limit_qps);
          setDailyLimit(s.rate_limit_daily ?? a.rate_limit_daily);
          setScope(s.visibility || a.visibility);
          setCompositionMode(s.composition_mode ?? a.composition_mode ?? 'chain');
          // 意图识别配置:草稿快照优先(未保存过则回退主表)
          if (s.intent_config) loadIntentConfig(s);
          setSelectedSkills((s.bound_skills || []).map((p: any) => ({ skill_id: p.skill_id, version: p.version ?? null })));
        } else {
          setPersonality(a.persona_desc || ''); setTone(a.tone_style || TONES[0]); setOpeningLine(a.opening_line || '');
          setSelectedSkills((a.bound_skill_ids || []).map((id: string) => ({ skill_id: id, version: null })));
        }
        // 版本下拉候选:有 draft 时基于草稿快照的绑定,否则基于主表
        const opts = await api.getPinOptions('agent', editId, draft?.id);
        const map: Record<string, api.PinOptionVersion[]> = {};
        opts.bindings.forEach((b) => { map[b.resource_id] = b.versions; });
        setSkillPinVersions(map);
      } catch (e: any) {
        toast.error(e.message || '加载失败');
      } finally {
        setLoading(false);
      }
    })();
  }, [editId]);

  // 详情页"编辑"按钮携带 focus 跳转:数据加载完成后滚动定位并高亮对应配置区块
  useEffect(() => {
    const focus = params.get('focus');
    if (!focus) return;
    const timer = setTimeout(() => {
      const el = document.getElementById(`focus-${focus}`);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('ring-2', 'ring-blue-400', 'ring-offset-2', 'rounded-2xl', 'transition-all');
        setTimeout(() => el.classList.remove('ring-2', 'ring-blue-400', 'ring-offset-2'), 2600);
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [params, loading]);

  const isSelected = (id: string) => selectedSkills.some((p) => p.skill_id === id);

  /** 该 skill 当前可选的最新 published 版本(派生默认填它) */
  const latestPinVersion = (skillId: string): number | null => {
    const list = skillPinVersions[skillId];
    if (!list || list.length === 0) return null;
    return list[0].version;
  };

  const toggleSkill = (id: string) =>
    setSelectedSkills((prev) =>
      prev.some((p) => p.skill_id === id)
        ? prev.filter((p) => p.skill_id !== id)
        : [...prev, { skill_id: id, version: latestPinVersion(id) }]);

  const setSkillPin = (skillId: string, version: number | null) =>
    setSelectedSkills((prev) => prev.map((p) => (p.skill_id === skillId ? { ...p, version } : p)));

  /** 刷新到最新依赖:有草稿走后端(同时固化快照),否则本地按最新 published 填 pin */
  const refreshPins = async () => {
    try {
      if (draftId) {
        const r = await api.refreshVersionPins(draftId);
        const pins = (r.snapshot?.bound_skills || []) as { skill_id: string; version: number | null }[];
        setSelectedSkills(pins.map((p) => ({ skill_id: p.skill_id, version: p.version ?? null })));
        toast.success(r.changed?.length ? `已刷新 ${r.changed.length} 个依赖:${r.changed.join(', ')}` : '依赖均已是最新');
      } else {
        setSelectedSkills((prev) => prev.map((p) => ({ ...p, version: latestPinVersion(p.skill_id) })));
        toast.success('已刷新为各依赖的最新 published 版本');
      }
    } catch (e: any) {
      toast.error(e.message || '刷新失败');
    }
  };

  /** 编辑区 snapshot:仅版本化字段 + 绑定 pin(v1.1.6 起 description 也是版本化字段,随发布/回滚) */
  const buildSnapshot = () => ({
    name, description, persona_desc: personality, tone_style: tone, opening_line: openingLine,
    visibility: scope, rate_limit_qps: qps, rate_limit_daily: dailyLimit,
    // v1.1.5 意图识别 agent 化:随版本快照一起发布/回滚
    intent_config: buildIntentConfig(),
    // v1.2:协作模式随版本快照(19 号 D4-①)
    composition_mode: compositionMode,
    bound_skills: selectedSkills,
  });

  /** 保存草稿:无 draft 时派生,否则仅更新 draft(版本号不变、不留痕) */
  const saveDraft = async (showToast = true): Promise<string> => {
    const snapshot = buildSnapshot();
    let id = draftId;
    if (id) {
      await api.editResourceVersion(id, { snapshot });
    } else {
      const r = await api.createResourceVersion('agent', editId!, { snapshot, note: '编辑区保存' });
      id = r.id;
      setDraftId(r.id);
      setVersionToken((t) => t + 1);
    }
    if (showToast) toast.success('草稿已保存(未发布,线上不受影响)');
    return id!;
  };

  const handleSave = async () => {
    if (!name.trim()) { toast.error('请填写 Agent 名称'); return; }
    setSaving(true);
    try {
      const payload = {
        name, description, avatar_url: avatarUrl || undefined, persona_desc: personality, tone_style: tone,
        opening_line: openingLine, bound_skill_ids: selectedSkills.map((p) => p.skill_id), rate_limit_qps: qps, rate_limit_daily: dailyLimit, visibility: scope,
        // v1.1.5 意图识别 agent 化:创建时同步提交 Agent 级意图识别配置
        intent_config: buildIntentConfig(),
        // v1.2:协作模式(19 号 D4-①)
        composition_mode: compositionMode,
      };
      await api.createAgent(payload);
      toast.success('Agent 已创建');
      navigate('/tools/agent');
    } catch (e: any) {
      toast.error(e.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  /** 编辑态:保存草稿(留在页面继续编辑) */
  const handleSaveDraft = async () => {
    if (!name.trim()) { toast.error('请填写 Agent 名称'); return; }
    setSaving(true);
    try {
      await saveDraft();
    } catch (e: any) {
      toast.error(e.message || '保存草稿失败');
    } finally {
      setSaving(false);
    }
  };

  /** 编辑态:发布 draft(先预检,再发布)。15 号:发布≠生效,零指派时后端返回黄条 hint 提醒去分发 */
  const handlePublish = async () => {
    if (!name.trim()) { toast.error('请填写 Agent 名称'); return; }
    setSaving(true);
    try {
      const id = draftId || (await saveDraft(false));
      // 推荐方案:发布预检(dry-run) —— 依赖闭包里有 DRAFT/已下线/不存在的 skill 会被拦截
      const pc = await api.precheckResourceVersion(id);
      if (!pc.pass) {
        const brief = pc.problems.slice(0, 3).join(';') +
          (pc.problems.length > 3 ? ` 等共 ${pc.problems.length} 项` : '');
        toast.error(`发布预检未通过:${brief}。请先处理后再发布。`, { duration: 8000 });
        setSaving(false);
        return;
      }
      const r = await api.publishResourceVersion(id);
      toast.success(r.message || `v${r.version} 已发布`);
      if (r.hint) toast.warning(r.hint, { duration: 10000 });
      navigate('/tools/agent');
    } catch (e: any) {
      toast.error(e.message || '发布失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  const filteredSkills = skills.filter((s) => !skillSearch || s.name.includes(skillSearch));

  return (
    <div className="max-w-5xl mx-auto space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex items-center gap-4 mb-2">
        <Button variant="ghost" size="icon" onClick={() => navigate('/tools/agent')} className="rounded-full"><ArrowLeft className="h-5 w-5" /></Button>
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">{isEdit ? '编辑 Agent' : '新建 Agent'}</h1>
          <p className="text-slate-500 text-sm">定义数字员工的人格、语气及核心技能，支持多技能灵活组合。</p>
        </div>
      </div>

      {isEdit && !draftId && hasPublished && (
        <div className="flex items-start gap-3 px-4 py-3 rounded-2xl bg-blue-50 border border-blue-200 text-blue-700 text-xs font-medium">
          <Info size={15} className="shrink-0 mt-0.5" />
          <span>
            该 Agent 已发布（线上版本冻结）。当前为<b>派生草稿模式</b>：此处修改会保存为草稿，<b>不影响线上</b>；
            确认后点「发布」生成新版本，再在分发中心把使用方<b>显式迁移</b>到新版本，旧版本保留可回滚。
          </span>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <div className="lg:col-span-2 space-y-8">
          <Card className="border-slate-200 shadow-sm overflow-hidden">
            <CardHeader className="border-b bg-slate-50/50 py-3 px-6"><CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500"><User size={16} /> 基础信息</CardTitle></CardHeader>
            <CardContent className="p-6 space-y-6">
              <div className="flex items-center gap-6 mb-2">
                <Avatar className="h-20 w-20 border-4 border-white shadow-xl rounded-3xl">
                  {avatarUrl && <AvatarImage src={avatarUrl} />}
                  <AvatarFallback className="bg-blue-600 text-white text-2xl font-black">{name[0] || 'A'}</AvatarFallback>
                </Avatar>
                <div className="flex-1 space-y-4">
                  <div className="space-y-1.5">
                    <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">Agent 名称</label>
                    <Input value={name} onChange={(e) => setName(e.target.value)} disabled={isEdit} placeholder="如：武夷山数字向导" className="h-11 rounded-xl" />
                  </div>
                </div>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">描述</label>
                <textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="简述该 Agent 的角色定位和主要职责"
                  className="w-full min-h-[80px] p-4 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 transition-all resize-none text-sm" />
                {isEdit && <p className="text-[11px] text-slate-400">描述随版本快照保存与发布（v1.1.6 起版本化），草稿不影响线上。</p>}
              </div>
            </CardContent>
          </Card>

          <Card className="border-slate-200 shadow-sm overflow-hidden">
            <CardHeader className="border-b bg-slate-50/50 py-3 px-6"><CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500"><Sparkles size={16} /> 人格设定</CardTitle></CardHeader>
            <CardContent className="p-6 space-y-6">
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
                <textarea value={openingLine} onChange={(e) => setOpeningLine(e.target.value)} placeholder="用户进入对话时看到的第一句话"
                  className="w-full min-h-[80px] p-4 rounded-xl border border-slate-200 focus:border-blue-500 focus:ring-4 focus:ring-blue-500/10 transition-all resize-none text-sm" />
              </div>
            </CardContent>
          </Card>

          <Card id="focus-skills" className="border-slate-200 shadow-sm overflow-hidden scroll-mt-24">
            <CardHeader className="border-b bg-slate-50/50 py-3 px-6 flex flex-row items-center justify-between">
              <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500"><Zap size={16} /> 绑定能力 (Skills)</CardTitle>
              {isEdit && (
                <Button size="sm" variant="outline" className="h-8 gap-1.5 text-xs"
                        disabled={selectedSkills.length === 0} onClick={refreshPins}>
                  <RefreshCw size={12} /> 刷新到最新依赖
                </Button>
              )}
            </CardHeader>
            <CardContent className="p-6">
              {/* v1.2(19 号 D4-①):协作模式收进 agent —— 绑定 >=2 个技能时决定它们怎么协作 */}
              <div className="mb-4 rounded-xl border border-slate-100 bg-slate-50/60 p-4">
                <div className="flex items-center justify-between mb-2">
                  <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">协作模式</label>
                  {selectedSkills.length < 2 && <span className="text-[10px] text-slate-400">绑定 2 个以上技能后生效</span>}
                </div>
                <div className="grid grid-cols-3 gap-2">
                  {([
                    { v: 'chain', t: '固定链', d: '按绑定顺序依次执行' },
                    { v: 'dag', t: '依赖图', d: '按顺序派生依赖,同层并发' },
                    { v: 'pool', t: '动态池', d: '工具合并,模型自主选择' },
                  ] as const).map((m) => (
                    <button key={m.v} type="button" onClick={() => setCompositionMode(m.v)}
                      className={cn('px-3 py-2 rounded-xl border text-left transition-all',
                        compositionMode === m.v ? 'bg-blue-600 text-white border-blue-600 shadow-md shadow-blue-100' : 'bg-white text-slate-600 border-slate-100 hover:border-slate-200')}>
                      <div className="text-xs font-black">{m.t}</div>
                      <div className={cn('text-[10px] mt-0.5', compositionMode === m.v ? 'text-blue-100' : 'text-slate-400')}>{m.d}</div>
                    </button>
                  ))}
                </div>
              </div>
              <div className="relative mb-4">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
                <Input value={skillSearch} onChange={(e) => setSkillSearch(e.target.value)} placeholder="搜索并绑定业务技能..." className="pl-9 h-11 rounded-xl" />
              </div>
              <div className="space-y-2 max-h-[300px] overflow-y-auto pr-2">
                {filteredSkills.map((skill) => {
                  const selected = isSelected(skill.id);
                  const pin = selectedSkills.find((p) => p.skill_id === skill.id);
                  return (
                    <div key={skill.id}>
                      <div onClick={() => toggleSkill(skill.id)}
                        className={cn('flex items-center justify-between p-4 rounded-xl border transition-all cursor-pointer',
                          selected ? 'border-blue-200 bg-blue-50/30 ring-1 ring-blue-100' : 'border-slate-100 bg-white hover:border-slate-200')}>
                        <div className="flex items-center gap-4">
                          <div className={cn('h-10 w-10 rounded-xl flex items-center justify-center shrink-0', selected ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-400')}>
                            <Cpu size={20} />
                          </div>
                          <div><div className="text-sm font-bold text-slate-800">{skill.name}</div><div className="text-[10px] text-slate-500 line-clamp-1">{skill.suggested_mode} · {skill.tool_ids?.length || 0} 个工具</div></div>
                        </div>
                        <div className={cn('h-5 w-5 rounded-full border-2 flex items-center justify-center transition-all', selected ? 'bg-blue-600 border-blue-600 text-white' : 'border-slate-200')}>
                          {selected && <CheckCircle2 size={12} />}
                        </div>
                      </div>
                      {selected && isEdit && (
                        <div className="flex items-center gap-2 rounded-b-xl border border-t-0 border-blue-100 bg-blue-50/40 px-4 py-2">
                          <label className="text-[11px] font-bold text-slate-500 shrink-0">版本 pin</label>
                          <select
                            value={pin?.version ?? ''}
                            onChange={(e) => setSkillPin(skill.id, e.target.value === '' ? null : Number(e.target.value))}
                            className="h-8 flex-1 rounded-lg border border-slate-200 bg-white px-2 text-xs font-bold text-slate-700 outline-none focus:border-blue-400">
                            <option value="">跟随默认(主表)</option>
                            {(skillPinVersions[skill.id] || []).map((v) => (
                              // v1.1.5:未发布版本也在下拉中可见(可被选到/预览),但 pin 只能钉已发布版本,
                              // 故 draft 选项 disabled 且标注「未发布」——发布后即变为可 pin 的正式版本。
                              <option key={v.version_id} value={v.version ?? ''} disabled={v.status === 'draft'}>
                                {v.status === 'draft'
                                  ? '未发布（发布后可用）'
                                  : `v${v.version}`}
                              </option>
                            ))}
                          </select>
                          <span className="text-[10px] text-slate-400 shrink-0">pin 到指定版本,不随该技能更新</span>
                        </div>
                      )}
                    </div>
                  );
                })}
                {filteredSkills.length === 0 && <div className="text-center text-xs text-slate-300 py-6">暂无可绑定的 Skill</div>}
              </div>
              {isEdit && (
                <p className="mt-3 text-[11px] text-slate-400">
                  版本语义(15 号):技能绑定需<b>显式钉版</b>(version:null 发布预检拦截);<b>保存</b>只写入草稿(线上不受影响),
                  <b>发布</b>冻结新快照,需在分发中心按用户指派才生效(无默认版本)。
                </p>
              )}
            </CardContent>
          </Card>

          <Card id="focus-intent" className="border-slate-200 shadow-sm overflow-hidden scroll-mt-24">
            <CardHeader className="border-b bg-slate-50/50 py-3 px-6">
              <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-500"><BrainCircuit size={16} /> 意图识别</CardTitle>
            </CardHeader>
            <CardContent className="p-6 space-y-5">
              <div className="flex items-start justify-between gap-4">
                <div className="space-y-1">
                  <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">启用意图识别</label>
                  <p className="text-[11px] text-slate-400 leading-relaxed">
                    意图识别是 Agent 级配置（一个 Agent 一个意图识别）：把用户请求路由/分配到本 Agent 绑定的技能。
                    关闭后按固定执行链直接跑。未发布保存即可生效于编辑区，发布后才作用于生产。
                  </p>
                </div>
                <button
                  onClick={() => setIntentEnabled(!intentEnabled)}
                  className={cn('relative h-6 w-11 shrink-0 rounded-full transition-colors', intentEnabled ? 'bg-blue-600' : 'bg-slate-300')}>
                  <span className={cn('absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all', intentEnabled ? 'left-[22px]' : 'left-0.5')} />
                </button>
              </div>

              {intentEnabled && (
                <div className="space-y-4">
                  <div>
                    <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">判定策略</label>
                    <select
                      value={intentMode}
                      onChange={(e) => setIntentMode(e.target.value)}
                      className="mt-1.5 h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-xs font-bold text-slate-700 outline-none focus:border-blue-400">
                      <option value="always">全开（默认）</option>
                      <option value="side_effects">仅存在写操作/高风险工具时</option>
                      <option value="off">关闭</option>
                    </select>
                  </div>
                  <div>
                    <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">意图分析模型（可选）</label>
                    <p className="text-[11px] text-slate-400 leading-relaxed">
                      用于本 Agent 意图识别/分诊的大模型，留空跟随框架配置；可从系统已启用的模型中选择，或自定义模型名。
                    </p>
                    <select
                      value={intentCustomModel || (intentModel && !models.some((m) => m.id === intentModel)) ? '__custom__' : intentModel}
                      onChange={(e) => {
                        const v = e.target.value;
                        if (v === '__custom__') { setIntentCustomModel(true); return; }
                        setIntentCustomModel(false);
                        setIntentModel(v);
                      }}
                      className="mt-1.5 h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-xs font-bold text-slate-700 outline-none focus:border-blue-400">
                      <option value="">跟随框架配置（默认）</option>
                      {models.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.name}{m.is_default ? '（默认）' : ''} · {m.model_id}
                        </option>
                      ))}
                      {intentModel && !models.some((m) => m.id === intentModel) ? (
                        <option value="__custom__">自定义：{intentModel}</option>
                      ) : (
                        <option value="__custom__">自定义模型名…</option>
                      )}
                    </select>
                    {(intentCustomModel || (intentModel && !models.some((m) => m.id === intentModel))) && (
                      <Input
                        value={intentModel}
                        onChange={(e) => setIntentModel(e.target.value)}
                        placeholder="输入模型名，如 doubao-seed-1-6-flash-250615"
                        className="mt-1.5 h-10 rounded-xl"
                      />
                    )}
                  </div>
                  <div className="flex items-start justify-between gap-4">
                    <div className="space-y-1">
                      <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">反问前二次确认</label>
                      <p className="text-[11px] text-slate-400 leading-relaxed">
                        判定需要反问前，重新完整通读能力清单复核缺失参数能否补齐；确认无法补齐才反问，降低误反问。
                      </p>
                    </div>
                    <button
                      onClick={() => setIntentRecheck(!intentRecheck)}
                      className={cn('relative h-6 w-11 shrink-0 rounded-full transition-colors', intentRecheck ? 'bg-blue-600' : 'bg-slate-300')}>
                      <span className={cn('absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all', intentRecheck ? 'left-[22px]' : 'left-0.5')} />
                    </button>
                  </div>
                  <div className="flex items-start justify-between gap-4">
                    <div className="space-y-1">
                      <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">会话历史注入（分诊历史轮数）</label>
                      <p className="text-[11px] text-slate-400 leading-relaxed">
                        分诊时注入最近 N 轮对话（用户原话+助手回答，含澄清追问），让智能体识别「在延续上轮话题/回答追问」，避免话题漂移。0 = 不注入（单轮隔离，默认）；多轮场景建议 2。
                      </p>
                    </div>
                    <Input
                      type="number"
                      min={0}
                      max={5}
                      value={intentHistoryRounds}
                      onChange={(e) => {
                        const raw = Number(e.target.value);
                        if (Number.isNaN(raw) || raw < 0) { setIntentHistoryRounds(0); return; }
                        setIntentHistoryRounds(Math.min(5, Math.floor(raw)));
                      }}
                      placeholder="0 = 不注入"
                      className="mt-1.5 h-10 w-24 shrink-0 rounded-xl px-3 text-center"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-bold text-slate-700 uppercase tracking-tighter">自定义分诊 Prompt（可选）</label>
                    <textarea
                      value={intentPrompt}
                      onChange={(e) => setIntentPrompt(e.target.value)}
                      placeholder="留空使用框架默认分诊模板。可定制本 Agent 的意图识别规则/话术，例如：遇到客流类请求必须挂载客流分析技能后再反问。"
                      rows={4}
                      className="mt-1.5 w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-xs text-slate-700 outline-none focus:border-blue-400 resize-y"
                    />
                  </div>
                  <p className="text-[11px] text-slate-400 leading-relaxed">
                    说明：意图识别配置随版本快照一起发布/回滚；未发布版本可被选择预览，只有已发布才在生产生效。
                  </p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="lg:col-span-1 space-y-6">
          <Card className="border-slate-200 shadow-xl overflow-hidden bg-slate-900 text-white sticky top-24">
            <CardHeader className="border-b border-slate-800 py-4"><CardTitle className="text-xs font-black uppercase tracking-[0.2em] text-slate-500 flex items-center gap-2"><Activity size={14} /> Live Preview</CardTitle></CardHeader>
            <CardContent className="p-6">
              <div className="flex flex-col items-center gap-4 mb-8">
                <Avatar className="h-24 w-24 border-4 border-slate-800 shadow-2xl rounded-3xl">{avatarUrl && <AvatarImage src={avatarUrl} />}<AvatarFallback className="bg-blue-600">{name[0] || 'A'}</AvatarFallback></Avatar>
                <div className="text-center">
                  <h3 className="text-xl font-black tracking-tight">{name || '未命名 Agent'}</h3>
                  <Badge className="mt-2 bg-blue-500/20 text-blue-400 border-none font-bold text-[10px]">READY TO DEPLOY</Badge>
                </div>
              </div>
              <div className="space-y-6">
                <div className="p-4 rounded-2xl bg-slate-800/50 border border-slate-700/50 relative">
                  <div className="absolute -top-2 left-4 px-2 bg-slate-900 text-[10px] font-black uppercase text-slate-500 tracking-widest">性格画像</div>
                  <p className="text-sm italic leading-relaxed text-slate-300">"{personality}"</p>
                </div>
                <div className="space-y-4">
                  <div className="flex items-center justify-between"><span className="text-[10px] font-bold text-slate-500 uppercase">语气</span><span className="text-xs font-black text-blue-400">{tone}</span></div>
                  <div className="flex items-center justify-between"><span className="text-[10px] font-bold text-slate-500 uppercase">已绑定技能</span><span className="text-xs font-black text-slate-100">{selectedSkills.length} SKILLS</span></div>
                  <div className="h-px bg-slate-800" />
                  <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-1"><label className="text-[10px] font-bold text-slate-500 uppercase tracking-tighter">QPS 限制</label><Input type="number" value={qps} onChange={(e) => setQps(Number(e.target.value) || 0)} className="h-9 bg-slate-800 border-slate-700 text-white rounded-lg text-sm" /></div>
                    <div className="space-y-1"><label className="text-[10px] font-bold text-slate-500 uppercase tracking-tighter">日调用上限</label><Input type="number" value={dailyLimit} onChange={(e) => setDailyLimit(Number(e.target.value) || 0)} className="h-9 bg-slate-800 border-slate-700 text-white rounded-lg text-sm" /></div>
                  </div>
                </div>
                <div className="h-px bg-slate-800" />
                <div className="space-y-4 pt-2">
                  <label className="text-[10px] font-bold text-slate-500 uppercase tracking-widest">可见范围</label>
                  <div className="grid grid-cols-2 gap-2">
                    {SCOPES.map((s) => (
                      <button key={s} onClick={() => setScope(s)} className={cn('px-2 py-2 rounded-lg text-[10px] font-black uppercase transition-all', scope === s ? 'bg-blue-600 text-white' : 'bg-slate-800 text-slate-500 hover:text-slate-300')}>{s}</button>
                    ))}
                  </div>
                </div>
              </div>
              {isEdit ? (
                <div className="mt-8 space-y-2">
                  <Button disabled={saving} variant="outline" className="w-full h-11 gap-2 rounded-2xl border-slate-700 bg-slate-800 text-slate-200 hover:bg-slate-700 hover:text-white font-bold text-sm transition-transform active:scale-95" onClick={handleSaveDraft}>
                    {saving ? <Loader2 size={18} className="animate-spin" /> : <Save size={18} />} 保存草稿(不生效)
                  </Button>
                  <Button disabled={saving} className="w-full h-14 bg-blue-600 hover:bg-blue-700 text-white font-black text-lg rounded-2xl shadow-2xl shadow-blue-900/40 gap-3 transition-transform active:scale-95" onClick={handlePublish}>
                    {saving ? <Loader2 size={22} className="animate-spin" /> : <Rocket size={24} />} 发布版本
                  </Button>
                  <p className="text-center text-[10px] text-slate-500">草稿仅写入编辑区;发布后需在分发中心按用户指派才生效(15 号无默认)</p>
                </div>
              ) : (
                <Button disabled={saving} className="w-full h-14 mt-8 bg-blue-600 hover:bg-blue-700 text-white font-black text-lg rounded-2xl shadow-2xl shadow-blue-900/40 gap-3 transition-transform active:scale-95" onClick={handleSave}>
                  {saving ? <Loader2 size={22} className="animate-spin" /> : <Rocket size={24} />} 创建 Agent
                </Button>
              )}
            </CardContent>
          </Card>

          {/* 版本管理:仅编辑已有 Agent 时出现 —— 新建时还没有资源 id */}
          {isEdit && editId && (
            <Card className="border-slate-200 shadow-sm mt-4">
              <CardContent className="p-5">
                <VersionPanel resourceType="agent" resourceId={editId} refreshToken={versionToken}
                              onChanged={() => window.location.reload()} />
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
};

export default AgentNew;
