import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Cpu, Building2, Plus, Loader2, ShieldCheck, RefreshCw, Info, Wallet, Coins, Users, Shield, KeyRound, Braces } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import UserCenter from '../UserCenter/UserCenter';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '../../components/ui/tabs';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { Modal, ConfirmModal } from '../../components/ui/PageHelpers';
import { useCapabilities } from '../../lib/capabilities';
import PlatformSecretsCard from '../../components/system/PlatformSecretsCard';
import SystemParamsPanel from '../../components/system/SystemParamsPanel';

// ─── 主组件 ────────────────────────────────────────────────
const SystemSettings: React.FC = () => {
  // 系统设置是集团级配置:集团三角色(含集团开发)可管,集团只读和景区角色不行。
  // 判定取后端的 capabilities,而不是在这里再列一遍角色名 —— 列表列漏了就是
  // 「后端放行、前端说无权访问」,用户只会觉得系统坏了。
  const cap = useCapabilities();
  const allowed = cap.manage_all;

  const [loading, setLoading] = useState(true);
  // ?tab=members 直达成员页签:用户管理并入之后,旧的 /user-center 链接转到这里,
  // 落地就该是成员列表而不是模型清单。
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState(searchParams.get('tab') || 'models');
  const switchTab = (v: string) => {
    setActiveTab(v);
    // 页签写进地址栏,刷新和分享链接都能回到同一个页签
    setSearchParams(v === 'models' ? {} : { tab: v }, { replace: true });
  };
  // 成员/角色/权限三个页签共用一个 UserCenter 实例(见该组件说明),
  // 所以它渲染在 TabsContent 之外,靠 activeTab 判断显示与否。
  const USER_TABS = ['members', 'roles', 'permissions'];
  const [modelTab, setModelTab] = useState('chat'); // 模型清单子页签: chat / embedding
  const [saving, setSaving] = useState(false);

  // ── 模型与预算状态 ──
  const [models, setModels] = useState<any[]>([]);
  const [budgets, setBudgets] = useState<any[]>([]);
  const [providers, setProviders] = useState<any[]>([]);
  // ── 模型单价配置(gov_llm_model_prices)──
  const [prices, setPrices] = useState<any[]>([]);
  const [priceDrafts, setPriceDrafts] = useState<Record<string, { input: string; output: string }>>({});
  const [editingPriceId, setEditingPriceId] = useState<string | null>(null); // 非空 = 该行处于编辑态
  const [newPrice, setNewPrice] = useState({ model_id: '', input: '', output: '' });
  const [savingPrice, setSavingPrice] = useState(false);
  const [modelModal, setModelModal] = useState(false);
  const [editingModel, setEditingModel] = useState<any>(null); // 非空 = 编辑模式,预填该行
  const [modelForm, setModelForm] = useState({ name: '', model_id: '', provider: 'openai', base_url: '', api_key: '', model_type: 'chat', input_price_cny_per_m: '', output_price_cny_per_m: '' });
  const [deleteTarget, setDeleteTarget] = useState<any>(null);
  const [budgetModal, setBudgetModal] = useState(false);
  const [budgetForm, setBudgetForm] = useState({ scope: 'group', scenic_id: '', daily_token_budget: 1000000, alert_threshold_pct: 80 });

  // ── 组织管理状态 ──
  // 景区列表来自景区主数据维表 dim_prd_scenic_base_yr_f(后端 /admin/settings/orgs 已切过去),
  // 这里只负责「开通/停用」和「登记」。名称/等级/属地都以主数据为准,页面不提供手工改名 ——
  // 手填的名字早晚和主数据对不上,而对不上时没人知道该信哪个。
  const [orgs, setOrgs] = useState<any[]>([]);
  const [orgTotal, setOrgTotal] = useState(0);
  const [orgKeyword, setOrgKeyword] = useState('');
  const [orgOnlyRegistered, setOrgOnlyRegistered] = useState(false);
  const [orgLoading, setOrgLoading] = useState(false);
  // 主数据有上万个景区,一次全拉既慢又没人会滚到底 —— 每页 50 条、滚到底自动续下一页。
  // 页码用 ref 而不是 state:滚动回调里要读最新值,state 会被闭包锁在旧值上,
  // 表现成「滚到底反复加载第 2 页」。
  const orgPageRef = useRef(1);
  const [orgLoadingMore, setOrgLoadingMore] = useState(false);
  const ORG_PAGE_SIZE = 50;
  const [orgModal, setOrgModal] = useState(false);
  // 登记弹窗:从主数据里挑(默认只列还没登记的),可多选
  const [catalog, setCatalog] = useState<api.ScenicCatalogItem[]>([]);
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [catalogKeyword, setCatalogKeyword] = useState('');
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);

  // ── 数据加载 ──
  const load = useCallback(async () => {
    setLoading(true);
    const rs = await Promise.allSettled([api.listModelConfigs(), api.listBudgetThresholds(), api.listBuiltinModelProviders(), api.listModelPrices()]);
    if (rs[0].status === 'fulfilled') setModels((rs[0].value as any).items);
    if (rs[1].status === 'fulfilled') setBudgets((rs[1].value as any).items);
    if (rs[2].status === 'fulfilled') setProviders((rs[2].value as any).items);
    if (rs[3].status === 'fulfilled') setPrices((rs[3].value as any).items);
    setLoading(false);
  }, []);

  useEffect(() => { if (allowed) load(); else setLoading(false); }, [allowed, load]);

  // 景区列表单独拉:它带筛选条件(关键字 / 只看已登记),跟着整页一起刷会有两个问题 ——
  // 一是每敲一个字就把模型、预算、单价全重拉一遍;二是筛选条件变了却不触发请求,
  // 表现成「勾了『只看已登记』列表没反应」。所以这里自己带防抖、自己管依赖。
  const loadOrgs = useCallback(async (page = 1, append = false) => {
    if (append) setOrgLoadingMore(true); else setOrgLoading(true);
    try {
      const r = await api.listScenicOrgs({
        keyword: orgKeyword.trim() || undefined,
        registered: orgOnlyRegistered || undefined,
        page, page_size: ORG_PAGE_SIZE,
      });
      // 续页用函数式更新:两次滚动挨得很近时,闭包里的 orgs 可能还是上一次的,
      // 直接拼接会把中间那一页覆盖掉
      setOrgs((prev) => (append ? [...prev, ...r.items] : r.items));
      setOrgTotal(r.total ?? r.items.length);
      orgPageRef.current = page;
    } catch (e: any) { toast.error(e.message || '景区列表读取失败'); }
    finally { setOrgLoading(false); setOrgLoadingMore(false); }
  }, [orgKeyword, orgOnlyRegistered]);

  useEffect(() => {
    if (!allowed) return;
    // 筛选条件变了就回到第 1 页 —— 不重置的话会拿新条件去取第 5 页,列表看着像空的
    orgPageRef.current = 1;
    const t = setTimeout(() => loadOrgs(1, false), 250);  // 防抖:每敲一个字都打一次接口会闪
    return () => clearTimeout(t);
  }, [allowed, loadOrgs]);

  const orgHasMore = orgs.length < orgTotal;

  /** 滚到底(还差 120px 时就开始)自动续下一页;正在加载或已到底则不动 */
  const onOrgScroll = (e: React.UIEvent<HTMLDivElement>) => {
    if (orgLoading || orgLoadingMore || !orgHasMore) return;
    const el = e.currentTarget;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
      loadOrgs(orgPageRef.current + 1, true);
    }
  };

  // 登记弹窗打开时拉一次候选池;关键字变了再拉
  // 候选池同样是上万行,分页拉 + 滚到底续页(页码用 ref,理由同景区列表)
  const catalogPageRef = useRef(1);
  const [catalogLoadingMore, setCatalogLoadingMore] = useState(false);

  const loadCatalog = useCallback(async (page = 1, append = false) => {
    if (append) setCatalogLoadingMore(true); else setCatalogLoading(true);
    try {
      const r = await api.getScenicCatalog({ keyword: catalogKeyword || undefined,
                                             registered: false, page, page_size: 50 });
      setCatalog((prev) => (append ? [...prev, ...r.items] : r.items));
      setCatalogTotal(r.total);
      catalogPageRef.current = page;
    } catch (e: any) { toast.error(e.message || '景区主数据读取失败'); }
    finally { setCatalogLoading(false); setCatalogLoadingMore(false); }
  }, [catalogKeyword]);

  const catalogHasMore = catalog.length < catalogTotal;
  const onCatalogScroll = (e: React.UIEvent<HTMLDivElement>) => {
    if (catalogLoading || catalogLoadingMore || !catalogHasMore) return;
    const el = e.currentTarget;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) {
      loadCatalog(catalogPageRef.current + 1, true);
    }
  };
  useEffect(() => {
    if (!orgModal) return;
    // 防抖:候选池直接查主数据(几百上千行),每敲一个字打一次接口,列表会闪
    catalogPageRef.current = 1;         // 换了关键字要回到第 1 页
    const t = setTimeout(() => loadCatalog(1, false), 250);
    return () => clearTimeout(t);
  }, [orgModal, loadCatalog]);

  const wrap = async (fn: () => Promise<any>, okMsg: string, close: () => void) => {
    setSaving(true);
    try { await fn(); toast.success(okMsg); close(); load(); loadOrgs(1, false); } catch (e: any) { toast.error(e.message || '操作失败'); }
    finally { setSaving(false); }
  };

  // 打开模型弹窗:编辑(m 非空,预填该行;api_key 留空=不修改)或新增(清空表单)
  const openModelModal = (m?: any, type?: string) => {
    if (m) {
      setEditingModel(m);
      setModelForm({ name: m.name, model_id: m.model_id || m.name, provider: m.provider, base_url: m.base_url || '', api_key: '', model_type: m.model_type || 'chat', input_price_cny_per_m: m.input_price_cny_per_m != null ? String(m.input_price_cny_per_m) : '', output_price_cny_per_m: m.output_price_cny_per_m != null ? String(m.output_price_cny_per_m) : '' });
    } else {
      setEditingModel(null);
      setModelForm({ name: '', model_id: '', provider: providers[0]?.id || 'openai', base_url: providers[0]?.default_base_url || '', api_key: '', model_type: type || 'chat', input_price_cny_per_m: '', output_price_cny_per_m: '' });
    }
    setModelModal(true);
  };
  const closeModelModal = () => { setModelModal(false); setEditingModel(null); };

  // 新增模型弹窗当前选中的厂商（用于联动默认 Base URL 与填写提示）
  const selectedProvider = providers.find((p) => p.id === modelForm.provider);

  // ── 模型单价配置（gov_llm_model_prices,替代 litellm 实时计费表）──
  const savePrice = async (modelId: string) => {
    const d = priceDrafts[modelId] || { input: '', output: '' };
    setSavingPrice(true);
    try {
      await api.upsertModelPrice({
        model_id: modelId,
        input_price_cny_per_m: d.input.trim() !== '' ? Number(d.input) : null,
        output_price_cny_per_m: d.output.trim() !== '' ? Number(d.output) : null,
      });
      toast.success(`「${modelId}」单价已保存`);
      // 清掉该行草稿并退出编辑态,避免刷新后残留草稿导致显示空值
      setPriceDrafts((prev) => { const n = { ...prev }; delete n[modelId]; return n; });
      setEditingPriceId(null);
      await load();
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSavingPrice(false); }
  };
  const addPrice = async () => {
    const id = newPrice.model_id.trim();
    if (!id) { toast.error('请填写或选择模型 ID'); return; }
    setSavingPrice(true);
    try {
      await api.upsertModelPrice({
        model_id: id,
        input_price_cny_per_m: newPrice.input.trim() !== '' ? Number(newPrice.input) : null,
        output_price_cny_per_m: newPrice.output.trim() !== '' ? Number(newPrice.output) : null,
      });
      toast.success(`「${id}」单价已添加`);
      setNewPrice({ model_id: '', input: '', output: '' });
      await load();
    } catch (e: any) { toast.error(e.message || '添加失败'); }
    finally { setSavingPrice(false); }
  };
  const removePrice = async (modelId: string) => {
    setSavingPrice(true);
    try {
      await api.deleteModelPrice(modelId);
      toast.success(`「${modelId}」单价已删除`);
      await load();
    } catch (e: any) { toast.error(e.message || '删除失败'); }
    finally { setSavingPrice(false); }
  };

  // 模型按类型拆分:对话模型 / 嵌入模型 各自维护、各自设默认
  const modelTypeLabel = (t: string) => (t === 'embedding' ? '嵌入模型' : '对话模型');
  const validModels = models.filter((m) => m.name && m.name.trim());
  const chatModels = validModels.filter((m) => (m.model_type || 'chat') === 'chat');
  const embeddingModels = validModels.filter((m) => m.model_type === 'embedding');
  // 单价配置的模型候选:有效模型 model_id + 已配置单价去重合并(依赖上方 validModels,勿上移)
  const priceModelOptions = Array.from(new Set([
    ...validModels.map((m) => m.model_id).filter(Boolean),
    ...prices.map((p) => p.model_id),
  ]));

  // 模型清单表格(按类型复用)
  const ModelTable = ({ type, items }: { type: string; items: any[] }) => {
    const label = modelTypeLabel(type);
    return (
      <Card className="border-slate-200 shadow-sm overflow-hidden">
        <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
          <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600 flex items-center gap-2"><Cpu size={15} />{label}清单（{items.length}）</CardTitle>
          <Button size="sm" className="bg-[#2563eb] h-9 gap-1.5 font-bold" onClick={() => openModelModal(undefined, type)}><Plus size={14} />新增{label}</Button>
        </CardHeader>
        <CardContent className="p-0 overflow-x-auto">
          <table className="enterprise-table">
            <thead><tr><th>模型名称</th><th>Provider</th><th>Base URL</th><th className="text-center">单价(入/出)</th><th className="text-center">API Key</th><th className="text-center">状态</th><th className="text-right">操作</th></tr></thead>
            <tbody>
              {items.map((m) => (
                <tr key={m.name}>
                  <td>
                    <div className="flex flex-col gap-0.5">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-bold text-slate-800">{m.name}</span>
                        {m.is_default && <Badge className={cn('text-[10px] font-bold', type === 'embedding' ? 'bg-violet-50 text-violet-600 hover:bg-violet-50' : 'bg-blue-50 text-blue-600 hover:bg-blue-50')}>默认</Badge>}
                      </div>
                      {m.model_id && m.model_id !== m.name && <span className="font-mono text-[10px] text-slate-400">ID: {m.model_id}</span>}
                    </div>
                  </td>
                  <td><Badge variant="outline" className="text-[10px]">{m.provider}</Badge></td>
                  <td><span className="font-mono text-[11px] text-slate-500">{m.base_url || '-'}</span></td>
                  <td className="text-center">
                    {m.input_price_cny_per_m != null || m.output_price_cny_per_m != null
                      ? <span className="font-mono text-[11px] text-slate-600">¥{m.input_price_cny_per_m ?? '?'}/{m.output_price_cny_per_m ?? '?'}</span>
                      : <span className="text-[10px] text-slate-300">未手填</span>}
                  </td>
                  <td className="text-center">{m.has_api_key ? <ShieldCheck size={14} className="text-emerald-500 mx-auto" /> : <span className="text-slate-300 text-xs">未设置</span>}</td>
                  <td className="text-center"><Badge variant="outline" className={cn('text-[10px] font-bold', m.active ? 'bg-emerald-50 text-emerald-600 border-emerald-100' : 'bg-slate-100 text-slate-400')}>{m.active ? '启用' : '禁用'}</Badge></td>
                  <td className="text-right whitespace-nowrap">
                    <Button variant="ghost" size="sm" className="h-8 text-[11px] font-bold text-slate-600" onClick={() => openModelModal(m)}>编辑</Button>
                    {!m.is_default && m.active && (
                      <Button variant="ghost" size="sm" className="h-8 text-[11px] font-bold text-blue-600" onClick={() => wrap(() => api.setDefaultModel(m.id), `已把 ${m.name} 设为默认${label},立即生效`, () => {})}>设为默认</Button>
                    )}
                    <Button variant="ghost" size="sm" className="h-8 text-[11px] font-bold text-slate-500" onClick={() => wrap(() => api.toggleModelConfig(m.id), '状态已更新', () => {})}>{m.active ? '停用' : '启用'}</Button>
                    <Button variant="ghost" size="sm" className="h-8 text-[11px] font-bold text-red-500" onClick={() => setDeleteTarget(m)}>删除</Button></td>
                </tr>
              ))}
              {items.length === 0 && <tr><td colSpan={7} className="py-12 text-center text-slate-300">{type === 'embedding' ? '未登记嵌入模型（向量入库/检索将使用 config.yaml 的 knowledge.embedding_model）' : '未登记对话模型（对话将使用 config.yaml 的 runtime.default_model）'}</td></tr>}
            </tbody>
          </table>
        </CardContent>
      </Card>
    );
  };

  if (!allowed) {
    return (
      <div className="flex flex-col items-center justify-center py-24 gap-3">
        <ShieldCheck size={40} className="text-slate-200" />
        <p className="text-sm text-slate-400">
          系统设置为集团级配置,仅集团管理员 / 集团开发者 / 超级管理员可访问
          {cap.role_label ? `(当前:${cap.role_label})` : ''}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">系统设置</h1>
          <p className="text-slate-500 text-sm mt-1">模型与预算 · 组织管理 · 系统成员 · 系统角色 · 权限管理。</p>
        </div>
        <Button variant="outline" className="h-10 rounded-xl border-slate-200 gap-2 font-medium" onClick={load}><RefreshCw size={15} />刷新</Button>
      </div>

      {loading ? <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div> : (
        <Tabs value={activeTab} onValueChange={switchTab} className="w-full">
          <TabsList className="bg-slate-100/50 p-1 border rounded-xl gap-1 flex-wrap h-auto">
            <TabsTrigger value="models" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><Cpu size={14} />模型与预算</TabsTrigger>
            <TabsTrigger value="params" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><Braces size={14} />参数配置</TabsTrigger>
            <TabsTrigger value="orgs" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><Building2 size={14} />组织管理</TabsTrigger>
            <TabsTrigger value="members" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><Users size={14} />系统成员</TabsTrigger>
            <TabsTrigger value="roles" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><Shield size={14} />系统角色</TabsTrigger>
            <TabsTrigger value="permissions" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs uppercase tracking-widest"><KeyRound size={14} />权限管理</TabsTrigger>
          </TabsList>

          {USER_TABS.includes(activeTab) && (
            <div className="mt-6">
              <UserCenter embedded tab={activeTab} onTabChange={switchTab} />
            </div>
          )}

          <div className="mt-6 space-y-6">
            {/* ═══════════════════════ 参数配置(0090 系统参数,Tool/MCP/Skill 用 {{参数}} 引用) ═══════════════════════ */}
            <TabsContent value="params" className="space-y-6">
              <SystemParamsPanel />
            </TabsContent>

            {/* ═══════════════════════ 模型与预算 ═══════════════════════ */}
            <TabsContent value="models" className="space-y-6">
              <div className="bg-blue-50/50 border border-blue-100 rounded-xl p-4 flex items-start gap-3">
                <Info size={16} className="text-blue-500 shrink-0 mt-0.5" />
                <p className="text-xs text-blue-800 leading-relaxed">这里登记的模型会<span className="font-bold">立即生效、无需重启进程</span>，优先级高于 config.yaml 里部署时固定的模型清单。API Key 加密存储，不会明文回显。</p>
              </div>

              {/* 模型清单:对话模型 / 嵌入模型 两个列表,各自添加、各自设默认 */}
              <Tabs value={modelTab} onValueChange={(v) => setModelTab(v)} className="w-full">
                <TabsList className="bg-slate-100/50 p-1 border rounded-xl gap-1 w-fit">
                  <TabsTrigger value="chat" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs">对话模型（{chatModels.length}）</TabsTrigger>
                  <TabsTrigger value="embedding" className="rounded-lg data-[state=active]:bg-white data-[state=active]:shadow-sm font-bold gap-2 px-5 py-2 text-xs">嵌入模型（{embeddingModels.length}）</TabsTrigger>
                </TabsList>
                <TabsContent value="chat" className="mt-4"><ModelTable type="chat" items={chatModels} /></TabsContent>
                <TabsContent value="embedding" className="mt-4"><ModelTable type="embedding" items={embeddingModels} /></TabsContent>
              </Tabs>

              {/* 数据平台 xpftkey:数据指标接口 / sys_supplier_ids 共用。以前只能改服务器环境变量 */}
              <PlatformSecretsCard />

              <Card className="border-slate-200 shadow-sm overflow-hidden">
                <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
                  <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600 flex items-center gap-2"><Wallet size={15} />预算护栏（{budgets.length}）</CardTitle>
                  <Button size="sm" className="bg-[#2563eb] h-9 gap-1.5 font-bold" onClick={() => setBudgetModal(true)}><Plus size={14} />新增预算</Button>
                </CardHeader>
                <CardContent className="p-0 overflow-x-auto">
                  <table className="enterprise-table">
                    <thead><tr><th>范围</th><th>每日 Token 上限</th><th>告警阈值</th></tr></thead>
                    <tbody>
                      {budgets.map((b) => (
                        <tr key={b.id}>
                          <td><span className="text-xs font-bold text-slate-700">{b.scope === 'group' ? '集团级' : `景区：${b.scenic_id}`}</span></td>
                          <td><span className="font-mono text-xs text-slate-600">{b.daily_token_budget.toLocaleString()}</span></td>
                          <td><span className="text-xs text-slate-600">{b.alert_threshold_pct}%</span></td>
                        </tr>
                      ))}
                      {budgets.length === 0 && <tr><td colSpan={3} className="py-12 text-center text-slate-300">未配置预算护栏（当前不限制）</td></tr>}
                    </tbody>
                  </table>
                </CardContent>
              </Card>

              {/* ── 模型单价配置(替代 litellm 实时拉取 GitHub 计费表) ── */}
              <Card className="border-slate-200 shadow-sm overflow-hidden">
                <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
                  <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600 flex items-center gap-2"><Coins size={15} />模型单价（元 / 百万 token）</CardTitle>
                </CardHeader>
                <CardContent className="p-0 overflow-x-auto">
                  <table className="enterprise-table">
                    <thead><tr><th>模型 ID</th><th>输入单价</th><th>输出单价</th><th className="text-right">操作</th></tr></thead>
                    <tbody>
                      {prices.map((p) => {
                        const editing = editingPriceId === p.model_id;
                        const d = editing ? (priceDrafts[p.model_id] || {
                          input: p.input_price_cny_per_m != null ? String(p.input_price_cny_per_m) : '',
                          output: p.output_price_cny_per_m != null ? String(p.output_price_cny_per_m) : '',
                        }) : null;
                        const startEdit = () => {
                          setPriceDrafts((prev) => ({ ...prev, [p.model_id]: {
                            input: p.input_price_cny_per_m != null ? String(p.input_price_cny_per_m) : '',
                            output: p.output_price_cny_per_m != null ? String(p.output_price_cny_per_m) : '',
                          } }));
                          setEditingPriceId(p.model_id);
                        };
                        const cancelEdit = () => {
                          setPriceDrafts((prev) => { const n = { ...prev }; delete n[p.model_id]; return n; });
                          setEditingPriceId(null);
                        };
                        return (
                          <tr key={p.model_id}>
                            <td><span className="font-mono text-[11px] text-slate-700">{p.model_id}</span></td>
                            {editing ? (
                              <>
                                <td><input value={d!.input} placeholder="兜底 15" onChange={(e) => setPriceDrafts({ ...priceDrafts, [p.model_id]: { ...d!, input: e.target.value } })} className="w-28 h-9 px-2 rounded-lg border border-slate-200 text-sm outline-none focus:border-blue-400" /></td>
                                <td><input value={d!.output} placeholder="兜底 15" onChange={(e) => setPriceDrafts({ ...priceDrafts, [p.model_id]: { ...d!, output: e.target.value } })} className="w-28 h-9 px-2 rounded-lg border border-slate-200 text-sm outline-none focus:border-blue-400" /></td>
                              </>
                            ) : (
                              <>
                                <td>{p.input_price_cny_per_m != null ? <span className="font-bold text-slate-700">¥{p.input_price_cny_per_m}</span> : <span className="text-xs text-slate-300">兜底</span>}</td>
                                <td>{p.output_price_cny_per_m != null ? <span className="font-bold text-slate-700">¥{p.output_price_cny_per_m}</span> : <span className="text-xs text-slate-300">兜底</span>}</td>
                              </>
                            )}
                            <td className="text-right whitespace-nowrap">
                              {editing ? (
                                <>
                                  <Button variant="outline" size="sm" className="h-8 text-[11px] font-bold text-emerald-600" disabled={savingPrice} onClick={() => savePrice(p.model_id)}>保存</Button>
                                  <Button variant="ghost" size="sm" className="h-8 text-[11px] font-bold" disabled={savingPrice} onClick={cancelEdit}>取消</Button>
                                </>
                              ) : (
                                <>
                                  <Button variant="ghost" size="sm" className="h-8 text-[11px] font-bold" disabled={savingPrice} onClick={startEdit}>编辑</Button>
                                  <Button variant="ghost" size="sm" className="h-8 text-[11px] font-bold text-red-500" disabled={savingPrice} onClick={() => removePrice(p.model_id)}>删除</Button>
                                </>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                      <tr>
                        <td>
                          <input list="model-price-options" value={newPrice.model_id} onChange={(e) => setNewPrice({ ...newPrice, model_id: e.target.value })} placeholder="输入或选择模型 ID" className="w-full min-w-44 h-9 px-2 rounded-lg border border-slate-200 text-sm outline-none focus:border-blue-400" />
                          <datalist id="model-price-options">{priceModelOptions.map((m) => <option key={m} value={m} />)}</datalist>
                        </td>
                        <td><input value={newPrice.input} onChange={(e) => setNewPrice({ ...newPrice, input: e.target.value })} placeholder="如 12" className="w-28 h-9 px-2 rounded-lg border border-slate-200 text-sm outline-none focus:border-blue-400" /></td>
                        <td><input value={newPrice.output} onChange={(e) => setNewPrice({ ...newPrice, output: e.target.value })} placeholder="如 12" className="w-28 h-9 px-2 rounded-lg border border-slate-200 text-sm outline-none focus:border-blue-400" /></td>
                        <td className="text-right"><Button size="sm" className="bg-[#2563eb] h-9 gap-1.5 font-bold" disabled={savingPrice} onClick={addPrice}><Plus size={13} />添加</Button></td>
                      </tr>
                      {prices.length === 0 && newPrice.model_id === '' && (
                        <tr><td colSpan={4} className="py-10 text-center text-slate-300">尚未配置单价（默认按兜底价估算）</td></tr>
                      )}
                    </tbody>
                  </table>
                  <div className="px-6 py-3 text-xs text-slate-400 border-t bg-slate-50/30">
                    单价保存在模型单价配置表，替代 litellm 实时拉取 GitHub 计费表；计费优先级：此表 &gt; 模型编辑里的手填单价 &gt; 兜底估算。保存后立即生效。
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            {/* ═══════════════════════ 组织管理 ═══════════════════════ */}
            <TabsContent value="orgs">
              <Card className="border-slate-200 shadow-sm overflow-hidden">
                <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between gap-3">
                  <CardTitle className="text-sm font-black uppercase tracking-widest text-slate-600">
                    {/* 显示的是命中总数,不是当前这一页的条数 —— 两者一混,「怎么只有 50 个」会查很久 */}
                    景区列表（{orgLoading ? '…' : `${orgs.length} / ${orgTotal}`}）
                  </CardTitle>
                  <div className="flex items-center gap-2">
                    <Input className="h-9 w-56 text-xs" placeholder="搜索景区名称 / ID" value={orgKeyword}
                      onChange={(e) => setOrgKeyword(e.target.value)} />
                    <label className="flex items-center gap-1.5 text-[11px] font-bold text-slate-500">
                      <input type="checkbox" checked={orgOnlyRegistered}
                        onChange={(e) => setOrgOnlyRegistered(e.target.checked)} />只看已登记
                    </label>
                    <Button size="sm" className="bg-[#2563eb] h-9 gap-1.5 font-bold" onClick={() => { setPicked([]); setOrgModal(true); }}><Plus size={14} />登记景区</Button>
                  </div>
                </CardHeader>
                {/* 只让列表这一块滚:标题、搜索框、表头都留在原地。
                    整页滚的话,滚到第 300 条时表头早没了,哪一列是「账号数」全靠数。 */}
                <CardContent className="p-0 max-h-[calc(100vh-340px)] min-h-[320px] overflow-y-auto overflow-x-auto"
                  onScroll={onOrgScroll}>
                  <table className="enterprise-table">
                    {/* sticky 落在每个 th 上而不是 thead:表格是 border-collapse,
                        粘在 thead 上时那条下边框在部分浏览器里不画,滚动时表头和数据会糊成一片。
                        用 inset 阴影补一条分隔线,滚多远都还看得出表头到哪儿为止。 */}
                    <thead className="sticky top-0 z-10 [&>tr>th]:sticky [&>tr>th]:top-0 [&>tr>th]:z-10 [&>tr>th]:bg-slate-50 [&>tr>th]:shadow-[inset_0_-1px_0_0_rgb(226,232,240)]">
                      <tr><th>景区 ID</th><th>景区名称</th><th>属地</th><th className="text-center">等级</th><th className="text-center">账号数</th><th className="text-center">平台服务</th><th className="text-right">操作</th></tr></thead>
                    <tbody>
                      {orgs.map((o) => (
                        <tr key={o.scenic_id}>
                          <td><span className="font-mono text-xs text-slate-500">{o.scenic_id}</span></td>
                          <td>
                            <span className="font-bold text-slate-800">{o.name}</span>
                            {/* 未登记 = 主数据里有、平台还没开过 —— 点「开通」会按主数据自动登记 */}
                            {!o.registered && <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-400">未登记</span>}
                          </td>
                          <td><span className="text-xs text-slate-500">{[o.province, o.city].filter(Boolean).join(' · ') || '—'}</span></td>
                          <td className="text-center"><span className="text-xs text-slate-500">{o.grade || '—'}</span></td>
                          <td className="text-center"><span className="text-xs text-slate-500">{o.user_count ?? 0}</span></td>
                          <td className="text-center"><Badge variant="outline" className={cn('text-[10px] font-bold', o.service_enabled ? 'bg-emerald-50 text-emerald-600 border-emerald-100' : 'bg-slate-100 text-slate-400')}>{o.service_enabled ? '已开通' : '已停用'}</Badge></td>
                          <td className="text-right">
                            <Button variant="ghost" size="sm" className="h-8 text-[11px] font-bold" onClick={() => wrap(() => api.toggleScenicOrg(o.scenic_id), '服务状态已更新', () => {})}>{o.service_enabled ? '停用' : '开通'}</Button></td>
                        </tr>
                      ))}
                      {orgLoading && <tr><td colSpan={7} className="py-12 text-center text-slate-300">加载中…</td></tr>}
                      {/* 滚到底会自动续页;按钮是兜底 —— 窗口很高、一页填不满时滚动事件不会触发 */}
                      {!orgLoading && orgHasMore && (
                        <tr><td colSpan={7} className="py-4 text-center">
                          {orgLoadingMore ? (
                            <span className="text-xs text-slate-300">加载中…</span>
                          ) : (
                            <button className="text-xs font-bold text-blue-600 hover:underline"
                              onClick={() => loadOrgs(orgPageRef.current + 1, true)}>
                              加载更多（已显示 {orgs.length} / {orgTotal}）
                            </button>
                          )}
                        </td></tr>
                      )}
                      {!orgLoading && !orgHasMore && orgs.length > 0 && (
                        <tr><td colSpan={7} className="py-4 text-center text-[11px] text-slate-300">
                          已到底部 · 共 {orgTotal} 个景区
                        </td></tr>
                      )}
                      {!orgLoading && orgs.length === 0 && (
                        <tr><td colSpan={7} className="py-12 text-center text-slate-300">
                          {orgKeyword || orgOnlyRegistered
                            ? '没有匹配的景区（换个关键字，或取消「只看已登记」）'
                            : '景区主数据表 dim_prd_scenic_base_yr_f 里还没有数据，请先由业务主数据同步写入'}
                        </td></tr>
                      )}
                    </tbody>
                  </table>
                  <div className="px-6 py-3 text-xs text-slate-400 border-t bg-slate-50/30">
                    景区名称、等级、属地来自景区主数据表 dim_prd_scenic_base_yr_f（由业务主数据同步），平台侧只维护开通状态。
                  </div>
                </CardContent>
              </Card>
            </TabsContent>
          </div>
        </Tabs>
      )}

      {/* ═══════════════════════ 弹窗区 ═══════════════════════ */}

      {/* 新增模型 */}
      {modelModal && (
        <Modal title={`${editingModel ? '编辑' : '新增'}${modelTypeLabel(modelForm.model_type)}`} onClose={closeModelModal}
          footer={<><Button variant="outline" onClick={closeModelModal}>取消</Button>
            <Button disabled={saving} className="bg-[#2563eb] gap-1.5" onClick={() => {
              if (!modelForm.name.trim()) return toast.error('请填写模型名称（展示用别名）');
              if (!modelForm.model_id.trim()) return toast.error('请填写模型 ID（供应商提供的调用 ID）');
              if (!editingModel && !modelForm.api_key.trim()) return toast.error('请填写 API Key');
              wrap(() => api.upsertModelConfig({
                ...modelForm,
                id: editingModel ? editingModel.id : undefined,
                // 单价留空 = 自动获取/兜底,填了按真实单价计费
                input_price_cny_per_m: modelForm.input_price_cny_per_m ? Number(modelForm.input_price_cny_per_m) : undefined,
                output_price_cny_per_m: modelForm.output_price_cny_per_m ? Number(modelForm.output_price_cny_per_m) : undefined,
              }), editingModel ? '模型配置已更新（立即生效）' : '模型配置已保存（立即生效）', closeModelModal);
            }}>
              {saving && <Loader2 size={14} className="animate-spin" />}保存</Button></>}>
          <div className="space-y-1.5"><label className="form-label">模型名称（展示用别名） <span className="text-red-500">*</span></label><Input value={modelForm.name} onChange={(e) => setModelForm({ ...modelForm, name: e.target.value })} placeholder="想叫什么就叫什么，如「豆包对话」" />
            <p className="text-[11px] text-slate-400">{editingModel ? '可改名，即时生效；历史任务归因按模型 ID，不受改名影响' : '仅用于在页面上展示和选择，不会传给模型服务商'}</p>
          </div>
          <div className="space-y-1.5"><label className="form-label">模型 ID <span className="text-red-500">*</span></label><Input value={modelForm.model_id} onChange={(e) => setModelForm({ ...modelForm, model_id: e.target.value })} placeholder={selectedProvider?.model_name_hint?.replace(/\*\*/g, '') || '如 deepseek-chat'} />
            <p className="text-[11px] text-slate-400">模型供应商提供的调用 ID（如 deepseek-chat / ep-xxx / doubao-embedding-...），运行时以此调用</p>
          </div>
          <div className="space-y-1.5"><label className="form-label">Provider</label>
            {providers.length > 0 ? (
              <>
                <select value={modelForm.provider} onChange={(e) => {
                  const p = providers.find((x) => x.id === e.target.value);
                  setModelForm((prev) => ({ ...prev, provider: e.target.value, base_url: p?.default_base_url ?? prev.base_url }));
                }} className="w-full h-10 px-3 rounded-lg border border-slate-200 text-sm outline-none">
                  {providers.map((p) => <option key={p.id} value={p.id}>{p.label}（{p.id}）</option>)}
                </select>
                {selectedProvider?.note && <p className="text-[11px] text-amber-600/80 leading-relaxed">{selectedProvider.note}</p>}
              </>
            ) : (
              <Input value={modelForm.provider} onChange={(e) => setModelForm({ ...modelForm, provider: e.target.value })} placeholder="litellm provider 前缀，如 openai / deepseek" />
            )}
          </div>
          <div className="space-y-1.5"><label className="form-label">Base URL</label><Input value={modelForm.base_url} onChange={(e) => setModelForm({ ...modelForm, base_url: e.target.value })} placeholder={selectedProvider?.default_base_url || 'https://.../v1'} />
            <p className="text-[11px] text-slate-400">官方默认地址已自动填充，私有化部署 / 专线可在此覆盖</p>
          </div>
          <div className="space-y-1.5"><label className="form-label">API Key {!editingModel && <span className="text-red-500">*</span>}</label><Input type="password" value={modelForm.api_key} onChange={(e) => setModelForm({ ...modelForm, api_key: e.target.value })} placeholder={editingModel ? '留空则不修改，保留原 Key' : '加密存储，不会明文回显'} />
            {editingModel && <p className="text-[11px] text-slate-400">{editingModel.has_api_key ? '当前已配置 Key，留空保存即保留原 Key' : '当前未配置 Key，可在此填写'}</p>}
          </div>
          <div className="space-y-1.5"><label className="form-label">模型类型</label>
            <select value={modelForm.model_type} onChange={(e) => setModelForm({ ...modelForm, model_type: e.target.value })} className="w-full h-10 px-3 rounded-lg border border-slate-200 text-sm outline-none">
              <option value="chat">对话模型（Agent 对话 / 任务执行用）</option>
              <option value="embedding">嵌入模型（向量知识入库 / 检索用）</option>
            </select>
            <p className="text-[11px] text-slate-400">每种类型可分别「设为默认」；类型变更不影响其他模型</p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5"><label className="form-label">输入单价（元/百万 token）</label><Input value={modelForm.input_price_cny_per_m} onChange={(e) => setModelForm({ ...modelForm, input_price_cny_per_m: e.target.value })} placeholder="留空自动获取（如 2）" inputMode="decimal" /></div>
            <div className="space-y-1.5"><label className="form-label">输出单价（元/百万 token）</label><Input value={modelForm.output_price_cny_per_m} onChange={(e) => setModelForm({ ...modelForm, output_price_cny_per_m: e.target.value })} placeholder="留空自动获取（如 8）" inputMode="decimal" /></div>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed">真实计费用途：留空时按 litellm 内置价格表自动匹配模型 ID（deepseek / GPT / Claude / Qwen 等主流模型），匹配不到用兜底估算价；手填后以此为准。</p>
        </Modal>
      )}

      {/* 删除模型二次确认 */}
      {deleteTarget && (
        <ConfirmModal
          title="删除模型"
          message={`确定要删除模型「${deleteTarget.name}」吗？删除后不可恢复，使用该模型的任务将无法调用。${deleteTarget.is_default ? `\n该模型当前为${modelTypeLabel(deleteTarget.model_type)}的默认，删除后该类型将回落到 config.yaml 配置。` : ''}`}
          confirmLabel="删除"
          danger
          loading={saving}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            if (!deleteTarget.id) return toast.error('模型标识异常，无法删除');
            wrap(() => api.deleteSettingsModel(deleteTarget.id), `已删除模型 ${deleteTarget.name}`, () => setDeleteTarget(null));
          }}
        />
      )}

      {/* 新增预算 */}
      {budgetModal && (
        <Modal title="新增预算护栏" onClose={() => setBudgetModal(false)}
          footer={<><Button variant="outline" onClick={() => setBudgetModal(false)}>取消</Button>
            <Button disabled={saving} className="bg-[#2563eb] gap-1.5" onClick={() => wrap(() => api.createBudgetThreshold({ ...budgetForm, scenic_id: budgetForm.scenic_id || null }), '预算护栏已保存', () => setBudgetModal(false))}>
              {saving && <Loader2 size={14} className="animate-spin" />}保存</Button></>}>
          <div className="space-y-1.5"><label className="form-label">范围</label>
            <select value={budgetForm.scope} onChange={(e) => setBudgetForm({ ...budgetForm, scope: e.target.value })} className="w-full h-10 px-3 rounded-lg border border-slate-200 text-sm outline-none">
              <option value="group">集团级</option><option value="scenic">景区级</option>
            </select>
          </div>
          {budgetForm.scope === 'scenic' && <div className="space-y-1.5"><label className="form-label">景区 ID</label><Input value={budgetForm.scenic_id} onChange={(e) => setBudgetForm({ ...budgetForm, scenic_id: e.target.value })} /></div>}
          <div className="space-y-1.5"><label className="form-label">每日 Token 上限（超过将熔断任务）</label><Input type="number" value={String(budgetForm.daily_token_budget)} onChange={(e) => setBudgetForm({ ...budgetForm, daily_token_budget: Number(e.target.value) || 0 })} /></div>
          <div className="space-y-1.5"><label className="form-label">告警阈值 %（仅记录告警，不阻断）</label><Input type="number" value={String(budgetForm.alert_threshold_pct)} onChange={(e) => setBudgetForm({ ...budgetForm, alert_threshold_pct: Number(e.target.value) || 0 })} /></div>
        </Modal>
      )}

      {/* 登记景区:从景区主数据里挑,不手填 —— 手敲的 ID/名称一旦有出入,
          后面所有按 scenic_id 的关联(用户、问题、Quick BI 连接)都会对不上 */}
      {orgModal && (
        <Modal title="登记景区" onClose={() => setOrgModal(false)}
          footer={<><Button variant="outline" onClick={() => setOrgModal(false)}>取消</Button>
            <Button disabled={saving || picked.length === 0} className="bg-[#2563eb] gap-1.5"
              onClick={() => wrap(() => api.registerScenicOrg({ scenic_ids: picked, enable: true }),
                                  `已登记 ${picked.length} 个景区`, () => { setOrgModal(false); setPicked([]); })}>
              {saving && <Loader2 size={14} className="animate-spin" />}登记（{picked.length}）</Button></>}>
          <div className="bg-slate-50 border border-slate-100 rounded-xl p-3 text-[11px] text-slate-500 leading-relaxed">
            候选来自景区主数据表 dim_prd_scenic_base_yr_f（业务主数据同步写入），下面只列还没登记的景区。登记后即开通平台服务，可随时停用。
          </div>
          <Input className="h-9 text-xs" placeholder="模糊搜索景区名称 / 别名 / ID（来自景区主数据）"
            value={catalogKeyword} onChange={(e) => setCatalogKeyword(e.target.value)} />
          <div className="max-h-80 overflow-y-auto rounded-xl border border-slate-100" onScroll={onCatalogScroll}>
            {catalogLoading && <div className="py-10 text-center"><Loader2 className="mx-auto animate-spin text-slate-300" /></div>}
            {!catalogLoading && catalog.length === 0 && (
              <div className="py-10 text-center text-xs text-slate-300">
                {catalogKeyword
                  ? `主数据里没有匹配「${catalogKeyword}」的未登记景区`
                  : '主数据里的景区都已登记（或主数据表还没有数据）'}
              </div>
            )}
            {!catalogLoading && catalog.map((c) => (
              <label key={c.scenic_id} className="flex cursor-pointer items-center gap-3 border-b border-slate-50 px-3 py-2 last:border-0 hover:bg-slate-50/60">
                <input type="checkbox" checked={picked.includes(c.scenic_id)}
                  onChange={(e) => setPicked(e.target.checked
                    ? [...picked, c.scenic_id]
                    : picked.filter((x) => x !== c.scenic_id))} />
                <div className="min-w-0 flex-1">
                  {/* 与绑定场景同一口径:景区ID（景区名称）—— 排查问题时对的是 ID */}
                  <div className="truncate text-sm text-slate-800">
                    <span className="font-mono text-xs text-slate-500">{c.scenic_id}</span>
                    <span className="ml-1 font-bold">（{c.scenic_name}）</span>
                    {c.scenic_grade_level && <span className="ml-2 text-[10px] font-normal text-amber-600">{c.scenic_grade_level}</span>}
                  </div>
                  <div className="truncate text-[11px] text-slate-400">
                    {[c.province_name, c.city_name].filter(Boolean).join(' ')}
                    {c.scenic_main_type ? ` · ${c.scenic_main_type}` : ''}
                    {c.scenic_alias ? ` · 别名 ${c.scenic_alias}` : ''}
                  </div>
                </div>
              </label>
            ))}
          </div>
          {catalogLoadingMore && <div className="py-1 text-center text-[11px] text-slate-300">加载中…</div>}
          <div className="text-[11px] text-slate-400">
            已显示 {catalog.length} / 待登记 {catalogTotal} 个（滚到底自动加载，也可用搜索缩小范围）。
            {picked.length > 0 && <span className="ml-1 text-blue-600">已勾选 {picked.length} 个</span>}
          </div>
        </Modal>
      )}
    </div>
  );
};

export default SystemSettings;
