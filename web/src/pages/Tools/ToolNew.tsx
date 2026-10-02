import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowLeft, Globe, Database, FileCode, Boxes, BarChart3, Save, Loader2, Info, ShieldAlert, Eye, RefreshCw, Rocket,
} from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { Badge } from '../../components/ui/badge';
import CodeEditor from '../../components/common/CodeEditor';
import { FieldLabel, KVEditor, ResponseSchemaEditor, SchemaEditor, Tutorial } from '../../components/common/FormBits';
import { QbiConnectionSelect, QbiFixedQuestionSelect, QbiTargetSelect } from './QbiToolFields';
import { cn } from '../../lib/utils';
import { useSearchParams } from 'react-router-dom';
import { useModalNavigate } from '../../lib/modalNav';
import { toast } from 'sonner';
import VersionPanel from '../../components/common/VersionPanel';
import ParamRefPicker from '../../components/common/ParamRefPicker';
import * as api from '../../lib/api';

const TYPE_ICON: Record<string, any> = { http: Globe, db: Database, python: FileCode, mcp: Boxes, quickbi: BarChart3 };
const TYPE_COLOR: Record<string, string> = {
  http: 'text-blue-500', db: 'text-emerald-500', python: 'text-amber-500', mcp: 'text-purple-500', quickbi: 'text-orange-500',
};

/**
 * 新建 / 编辑工具。
 *
 * 表单结构完全由后端下发的模板驱动(GET /admin/tools/templates)——字段、说明、示例、
 * 教程都不在前端硬编码。原因是真正消费 type_config 的是执行引擎和沙箱,写在前端会变成
 * 两份真相:后端加了字段前端不知道,前端写的说明和实际行为对不上。
 */
const ToolNew: React.FC = () => {
  const navigate = useModalNavigate(); // 在弹窗里打开时,跳详情/编辑留在弹窗、跳回列表即关闭
  const [params] = useSearchParams();
  const editId = params.get('id');
  const isEdit = !!editId;

  const [tpl, setTpl] = useState<api.ToolTemplatesResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [draftId, setDraftId] = useState<string | null>(null);
  const [versionToken, setVersionToken] = useState(0);

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [type, setType] = useState('http');
  const [riskLevel, setRiskLevel] = useState<'low' | 'medium' | 'high'>('low');
  const [visibility, setVisibility] = useState('private');
  const [cfg, setCfg] = useState<Record<string, any>>({});

  const [datasources, setDatasources] = useState<any[]>([]);
  const [preview, setPreview] = useState<any>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewParams, setPreviewParams] = useState('{}');
  // 当前展开的配置分区。字段多了堆成一长条很难找,按功能分区导航
  const [activeSection, setActiveSection] = useState('basic');

  const template = useMemo(() => tpl?.templates.find((t) => t.type === type), [tpl, type]);

  useEffect(() => {
    (async () => {
      try {
        const t = await api.listToolTemplates();
        setTpl(t);
        if (!isEdit) {
          const def = t.templates.find((x) => x.type === 'http');
          setCfg(Object.fromEntries((def?.fields || []).map((f) => [f.key, f.default])));
        }
      } catch (e: any) { toast.error(e.message || '加载工具模板失败'); }
      // 数据源用于 db 类型的下拉;拿不到不阻断(可能没权限或还没配)
      api.listDatasources().then((r) => setDatasources(r.items)).catch(() => setDatasources([]));

      if (isEdit) {
        try {
          const [t, versions]: any = await Promise.all([api.getTool(editId!), api.listResourceVersions('tool', editId!)]);
          setName(t.name); setType(t.type);
          const draft = versions.items.find((v: any) => v.status === 'draft');
          if (draft) {
            // 编辑区 = draft:已保存的草稿优先于主表,保证不丢未发布改动
            setDraftId(draft.id);
            const s = draft.snapshot || {};
            setDescription(s.description ?? t.description ?? '');
            setRiskLevel(s.risk_level || t.risk_level || 'low');
            setVisibility(s.visibility || t.visibility || 'private');
            setCfg(s.type_config || t.type_config || {});
          } else {
            setDescription(t.description || ''); setRiskLevel(t.risk_level);
            setVisibility(t.visibility); setCfg(t.type_config || {});
          }
        } catch (e: any) { toast.error(e.message || '加载工具详情失败'); }
      }
      setLoading(false);
    })();
  }, [editId]);

  /** 切换类型时用该类型的默认值重置配置——不同类型的字段完全不同,留着上一个类型的
   *  残留字段只会在保存时产生一堆无意义的键。编辑态不允许改类型,所以不会误清。 */
  const switchType = (next: string) => {
    setType(next);
    setPreview(null);
    setActiveSection('basic');
    const f = tpl?.templates.find((x) => x.type === next)?.fields || [];
    setCfg(Object.fromEntries(f.map((x) => [x.key, x.default])));
  };

  const setField = (key: string, v: any) => setCfg((prev) => ({ ...prev, [key]: v }));

  /** 选了骨架:代码和入参定义一起换。
   *  只换代码的话,入参定义还是上一套的,模型按旧 schema 传参、脚本按新骨架读参,
   *  跑起来是「参数拿不到」这种很难联想到原因的错。 */
  const handlePickSkeleton = (id: string, skeletons?: api.ToolField['skeletons']) => {
    const sk = (skeletons || []).find((x) => x.id === id);
    if (sk?.input_schema) {
      setCfg((prev) => ({ ...prev, input_schema: sk.input_schema }));
      toast.info(`已套用「${sk.label}」骨架,入参定义也一并更新`);
    }
  };

  const doPreview = async () => {
    let p: any = {};
    try { p = JSON.parse(previewParams || '{}'); }
    catch { toast.error('预览参数需要是合法 JSON'); return; }
    setPreviewing(true);
    try { setPreview(await api.previewToolRequest({ type, type_config: cfg, params: p })); }
    catch (e: any) { toast.error(e.message || '预览失败'); }
    finally { setPreviewing(false); }
  };

  const validate = () => {
    if (!name.trim()) { toast.error('请填写工具名称'); return false; }
    for (const f of template?.fields || []) {
      if (f.required && !cfg[f.key]) { toast.error(`「${f.label}」是必填项`); return false; }
    }
    return true;
  };

  /** 编辑区 snapshot:tool 版本化字段(description/type_config/risk_level/visibility,type/name 是标识不可改) */
  const buildSnapshot = () => ({ description, type, type_config: cfg, risk_level: riskLevel, visibility });

  /** 保存草稿:无 draft 时派生,否则仅更新 draft(版本号不变、不留痕) */
  const saveDraft = async (showToast = true): Promise<string> => {
    const snapshot = buildSnapshot();
    let id = draftId;
    if (id) {
      await api.editResourceVersion(id, { snapshot });
    } else {
      const r = await api.createResourceVersion('tool', editId!, { snapshot, note: '编辑区保存' });
      id = r.id;
      setDraftId(r.id);
      setVersionToken((t) => t + 1);
    }
    if (showToast) toast.success('草稿已保存(未发布,线上不受影响)');
    return id!;
  };

  /** 新建:走老 createTool */
  const handleCreate = async () => {
    if (!validate()) return;
    setSaving(true);
    try {
      await api.createTool({ name, description, type, risk_level: riskLevel, type_config: cfg, visibility });
      toast.success('工具已创建');
      navigate('/tools/tool');
    } catch (e: any) { toast.error(e.message || '保存失败'); }
    finally { setSaving(false); }
  };

  /** 编辑态:保存草稿(留在页面继续编辑) */
  const handleSaveDraft = async () => {
    if (!validate()) return;
    setSaving(true);
    try {
      await saveDraft();
    } catch (e: any) { toast.error(e.message || '保存草稿失败'); }
    finally { setSaving(false); }
  };

  /** 编辑态:发布 draft(15 号文档:无默认版本概念,发布仅生成 published 快照,不隐含对任何人生效;
   *   Tool 无需用户指派,由绑定它的 Agent 间接生效;后端 D7-A 黄条 hint 仅在需要时提醒) */
  const handlePublish = async () => {
    if (!validate()) return;
    setSaving(true);
    try {
      const id = draftId || (await saveDraft(false));
      const r = await api.publishResourceVersion(id);
      toast.success(r.message || (r.version != null ? `v${r.version} 已发布` : '发布成功'));
      if (r.hint) toast.warning(r.hint);
      navigate('/tools/tool');
    } catch (e: any) { toast.error(e.message || '发布失败'); }
    finally { setSaving(false); }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;

  const Icon = TYPE_ICON[type] || Globe;

  return (
    <div className="mx-auto max-w-[1400px] space-y-6 pb-12">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" className="rounded-full" onClick={() => navigate('/tools/tool')}>
          <ArrowLeft size={18} />
        </Button>
        <div>
          <h1 className="text-xl font-bold text-slate-900">{isEdit ? '编辑 Tool' : '新建 Tool'}</h1>
          <p className="mt-0.5 text-xs text-slate-500">{template?.summary}</p>
        </div>
      </div>

      {isEdit && (
        <div className="flex items-center gap-3 rounded-xl border border-amber-100 bg-amber-50 p-4 text-amber-700">
          <Info size={16} className="shrink-0" />
          <p className="text-sm">编辑区为草稿:保存不生效、不留痕;<b>发布版本</b>后生成新版本,原版本保留在版本历史中,可随时回滚。15 号文档:无默认版本概念,发布不隐含对任何人生效,Tool 作为 Agent 依赖被引用时生效。</p>
        </div>
      )}

      {/* ── 基础信息 ── */}
      <Card className="border-slate-200 shadow-sm">
        <CardContent className="space-y-5 p-6">
          {/* 宽屏下横向铺开:名称/可见范围一行,描述独占一行(它最长),
              类型与风险等级一行 —— 与下方三栏同宽,视觉上不会断层 */}
          <div className="grid grid-cols-2 gap-5">
            <div>
              <FieldLabel label="工具名称" required
                hint="租户范围内唯一。名称会被规范化成模型侧的函数名(中文会转成 tool_xxx,原名写进描述里),所以叫中文完全没问题。" />
              <Input value={name} onChange={(e) => setName(e.target.value)} disabled={isEdit} placeholder="如:查询订单记录" />
            </div>
            <div>
              <FieldLabel label="可见范围"
                hint="private=仅创建者;scenic=本景区;group=集团内;public=全平台。决定哪些 Skill 能绑定它。" />
              <select value={visibility} onChange={(e) => setVisibility(e.target.value)}
                      className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none focus:border-blue-400">
                <option value="private">private(仅创建者)</option>
                <option value="scenic">scenic(本景区)</option>
                <option value="group">group(集团内)</option>
                <option value="public">public(全平台)</option>
              </select>
            </div>
          </div>

          <div>
            <FieldLabel label="工具描述" required
              hint="**这段会直接进入大模型的 function-calling schema**,模型靠它判断什么时候该调用这个工具。写清楚用途和适用场景,不要只写一个名词。" />
            <Input value={description} onChange={(e) => setDescription(e.target.value)}
                   placeholder="如:按景区和日期范围查询订单明细,用于客流与营收分析" />
          </div>

          <div className="grid grid-cols-2 gap-5">
          <div>
            <FieldLabel label="工具类型" required hint="创建后不可更改——不同类型的执行路径完全不同。" />
            <div className="flex gap-2">
              {(tpl?.templates || []).filter((t) => t.type !== 'mcp').map((t) => {
                const I = TYPE_ICON[t.type] || Globe;
                return (
                  <button key={t.type} type="button" disabled={isEdit} onClick={() => switchType(t.type)}
                    className={cn('flex flex-1 items-center justify-center gap-2 rounded-xl border py-2.5 text-sm font-medium transition-all disabled:opacity-50',
                      type === t.type ? 'border-blue-200 bg-blue-50 text-blue-600' : 'border-slate-200 text-slate-500 hover:bg-slate-50')}>
                    <I size={15} className={type === t.type ? TYPE_COLOR[t.type] : 'text-slate-400'} />{t.label}
                  </button>
                );
              })}
            </div>
            {type === 'mcp' && <p className="mt-2 text-[11px] text-amber-600">MCP 工具应通过「工具中心 · MCP」发现导入,不建议手工创建。</p>}
          </div>

          <div>
            <FieldLabel label="风险等级" required
              hint="high 会在每次执行前强制触发人工审批(HITL),任务暂停并进入审批中心。涉及资金、写操作、对外发送的工具建议设为 high。" />
            <div className="flex gap-2">
              {(['low', 'medium', 'high'] as const).map((lv) => (
                <button key={lv} type="button" onClick={() => setRiskLevel(lv)}
                  className={cn('flex-1 rounded-lg border py-2 text-xs font-bold uppercase transition-all',
                    riskLevel === lv
                      ? lv === 'high' ? 'border-rose-200 bg-rose-50 text-rose-600'
                        : lv === 'medium' ? 'border-amber-200 bg-amber-50 text-amber-600'
                        : 'border-emerald-200 bg-emerald-50 text-emerald-600'
                      : 'border-slate-200 text-slate-400 hover:bg-slate-50')}>
                  {lv}
                </button>
              ))}
            </div>
            {riskLevel === 'high' && (
              <p className="mt-2 flex items-center gap-1.5 text-[11px] text-rose-600">
                <ShieldAlert size={12} />每次调用都会暂停任务、等待人工审批通过后才继续执行
              </p>
            )}
          </div>
          </div>
        </CardContent>
      </Card>

      {/* ── 类型专属配置:左侧分区导航 + 中间表单 + 右侧实时预览 ── */}
      <div className="grid grid-cols-12 gap-4">
        {/* 左:分区导航。字段多了之后堆成一长条很难找,按功能分区更好定位 */}
        <div className="col-span-2">
          <div className="sticky top-4 space-y-1.5">
            {(template?.sections || []).map((sec) => {
              const count = (template?.fields || []).filter((f) => (f.section || 'basic') === sec.id).length;
              return (
                <button key={sec.id} type="button" onClick={() => setActiveSection(sec.id)}
                  className={cn('w-full rounded-xl border px-3 py-2.5 text-left transition-all',
                    activeSection === sec.id
                      ? 'border-blue-200 bg-blue-50 text-blue-700'
                      : 'border-transparent text-slate-500 hover:bg-slate-50')}>
                  <div className="text-[13px] font-semibold">{sec.label}</div>
                  <div className="mt-0.5 text-[10px] leading-snug opacity-70">{sec.desc}</div>
                  {count > 0 && <div className="mt-1 text-[10px] opacity-50">{count} 项</div>}
                </button>
              );
            })}
          </div>
        </div>

        {/* 中:当前分区的字段 */}
        <div className="col-span-6">
          <Card className="border-slate-200 shadow-sm">
            <div className="flex items-center gap-2 border-b border-slate-100 bg-slate-50/60 px-5 py-3">
              <Icon size={15} className={TYPE_COLOR[type]} />
              <span className="text-sm font-bold text-slate-700">
                {(template?.sections || []).find((x) => x.id === activeSection)?.label || template?.label}
              </span>
              {/* 系统参数引用:复制 {{参数}} 粘贴到地址 / 请求头 / SQL,或写成参数定义里某个参数的 default */}
              <ParamRefPicker className="ml-auto" />
            </div>
            <CardContent className="space-y-5 p-5">
              {(template?.fields || [])
                .filter((f) => (f.section || 'basic') === activeSection)
                .map((f) => (
                  <div key={f.key}>
                    <FieldLabel label={f.label} hint={f.description} required={f.required} />
                    {renderWidget(f, cfg[f.key], (v) => setField(f.key, v), datasources, navigate, handlePickSkeleton)}
                  </div>
                ))}
              {!(template?.fields || []).some((f) => (f.section || 'basic') === activeSection) && (
                <p className="py-6 text-center text-xs text-slate-300">该分区暂无需要配置的项</p>
              )}
            </CardContent>
          </Card>
        </div>

        {/* 右:请求预览。跟表单并排,改完立刻能看到组装结果,不用来回滚动 */}
        <div className="col-span-4">
          <Card className="sticky top-4 border-slate-200 shadow-sm">
            <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/60 px-4 py-3">
              <div className="flex items-center gap-1.5">
                <Eye size={14} className="text-slate-500" />
                <span className="text-[13px] font-bold text-slate-700">请求预览</span>
              </div>
              <Button size="sm" variant="outline" onClick={doPreview} disabled={previewing}
                      className="h-7 gap-1 px-2 text-[11px]">
                {previewing ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}刷新
              </Button>
            </div>
            <CardContent className="space-y-3 p-4">
              <div>
                <div className="mb-1 text-[11px] font-semibold text-slate-500">模拟入参(JSON)</div>
                <textarea value={previewParams} onChange={(e) => setPreviewParams(e.target.value)} rows={3}
                  spellCheck={false}
                  className="w-full rounded-lg border border-slate-200 p-2 font-mono text-[11px] outline-none focus:border-blue-400" />
              </div>

              {preview ? (
                <>
                  <div>
                    <div className="mb-1 text-[11px] font-semibold text-slate-500">
                      {preview.kind === 'db' ? '实际执行的 SQL' : preview.kind === 'mcp' ? 'JSON-RPC 报文' : '实际发出的请求'}
                    </div>
                    <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-[#0d1117] p-3 font-mono text-[11px] leading-relaxed text-emerald-300">
                      {preview.raw}
                    </pre>
                  </div>

                  {preview.query && Object.keys(preview.query).length > 0 && (
                    <div>
                      <div className="mb-1 text-[11px] font-semibold text-slate-500">查询参数</div>
                      <div className="rounded-lg bg-slate-50 p-2 font-mono text-[11px]">
                        {Object.entries(preview.query).map(([k, v]) => (
                          <div key={k}><span className="text-sky-600">{k}</span> = {JSON.stringify(v)}</div>
                        ))}
                      </div>
                    </div>
                  )}

                  {preview.bind_values && (
                    <div>
                      <div className="mb-1 text-[11px] font-semibold text-slate-500">绑定参数</div>
                      <div className="rounded-lg bg-slate-50 p-2">
                        {preview.bind_values.map((b: any) => (
                          <div key={b.index} className="flex items-center gap-1.5 py-0.5 font-mono text-[11px]">
                            <span className="text-sky-600">{b.index}</span>
                            <span className="text-slate-400">=</span>
                            <span className="truncate text-slate-700">{JSON.stringify(b.value)}</span>
                            <Badge variant="outline" className="ml-auto shrink-0 text-[9px]">{b.source}</Badge>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <div>
                    <div className="mb-1 text-[11px] font-semibold text-slate-500">返回结果</div>
                    <div className="rounded-lg border border-dashed border-slate-200 bg-slate-50/50 p-3 text-[11px] leading-relaxed text-slate-400">
                      预览只渲染不发起真实调用,所以这里没有返回值。
                      保存后可在工具详情页「试运行」中发起一次真实调用查看返回结果。
                    </div>
                  </div>

                  <ul className="space-y-0.5">
                    {(preview.notes || []).map((n: string, i: number) => (
                      <li key={i} className="flex gap-1 text-[10px] leading-relaxed text-slate-400">
                        <span className="text-slate-300">•</span>{n}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <div className="rounded-lg border-2 border-dashed border-slate-100 py-8 text-center text-[11px] text-slate-300">
                  点「刷新」查看组装后的请求
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      {/* 版本管理:只在编辑已有工具时出现 —— 新建时还没有资源 id,无从谈版本 */}
      {isEdit && editId && (
        <Card className="border-slate-200 shadow-sm">
          <CardContent className="p-5">
            <VersionPanel resourceType="tool" resourceId={editId} refreshToken={versionToken} onChanged={() => window.location.reload()} />
          </CardContent>
        </Card>
      )}

      {template?.tutorial && <Tutorial items={template.tutorial} />}

      {isEdit ? (
        <div className="flex gap-3">
          <Button variant="outline" className="flex-1" onClick={() => navigate('/tools/tool')}>取消</Button>
          <Button variant="outline" onClick={handleSaveDraft} disabled={saving} className="flex-1 gap-2 border-slate-700 bg-slate-800 text-slate-200 hover:bg-slate-700 hover:text-white">
            {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}保存草稿(不生效)
          </Button>
          <Button onClick={handlePublish} disabled={saving} className="flex-1 gap-2 bg-[#1677ff] hover:bg-[#4096ff]">
            {saving ? <Loader2 size={15} className="animate-spin" /> : <Rocket size={15} />}发布版本
          </Button>
        </div>
      ) : (
        <div className="flex gap-3">
          <Button variant="outline" className="flex-1" onClick={() => navigate('/tools/tool')}>取消</Button>
          <Button onClick={handleCreate} disabled={saving} className="flex-1 gap-2 bg-[#1677ff] hover:bg-[#4096ff]">
            {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}创建工具
          </Button>
        </div>
      )}
    </div>
  );
};

/** 按模板里的 widget 类型渲染控件。新增字段类型只要后端模板加一个 widget 值,这里补一个分支。 */
function renderWidget(f: api.ToolField, value: any, onChange: (v: any) => void, datasources: any[], navigate: any,
                      onPickSkeleton?: (id: string, skeletons?: api.ToolField['skeletons']) => void,
                      siblingConnectionId?: string) {
  switch (f.widget) {
    case 'select':
      return (
        <select value={value ?? f.default} onChange={(e) => onChange(e.target.value)}
                className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none focus:border-blue-400">
          {/* 有中文名就显示中文:`dataset_qa` 这种值只有开发看得懂,运营选的时候全靠猜。
              存的仍是原值,只改显示。 */}
          {(f.options || []).map((o) => <option key={o} value={o}>{f.option_labels?.[o] || o}</option>)}
        </select>
      );

    case 'switch':
      return (
        <label className="flex cursor-pointer items-center gap-2">
          <input type="checkbox" checked={value ?? f.default ?? true} onChange={(e) => onChange(e.target.checked)}
                 className="h-4 w-4 rounded border-slate-300 text-blue-600" />
          <span className="text-sm text-slate-600">{value ?? f.default ? '允许' : '不允许'}</span>
        </label>
      );

    case 'kv':
      return <KVEditor value={value || {}} onChange={onChange} />;

    case 'schema':
      return <SchemaEditor value={value ?? f.default} onChange={onChange} example={f.example} />;

    case 'response':
      return <ResponseSchemaEditor value={value ?? f.default} onChange={onChange} />;

    case 'code':
      return (
        <CodeEditor
          value={value ?? f.default ?? ''} onChange={onChange}
          language={(f.language as any) || 'python'}
          skeletons={f.skeletons}
          onPickSkeleton={(id) => onPickSkeleton?.(id, f.skeletons)}
        />
      );

    case 'sql':
      return <CodeEditor value={value ?? f.default ?? ''} onChange={onChange} language="sql" minRows={8} />;

    case 'qbi-connection-select':
      return <QbiConnectionSelect value={value ?? ''} onChange={onChange} placeholder={f.placeholder} />;

    case 'qbi-target-select':
      // 连接 ID 透传进来:固定问题按连接过滤(目标数据刻意不过滤,见组件里的说明)
      return <QbiTargetSelect value={value ?? f.default} onChange={onChange} connectionId={siblingConnectionId} />;

    case 'qbi-fixed-question':
      return <QbiFixedQuestionSelect value={value ?? ''} onChange={onChange} connectionId={siblingConnectionId} />;

    case 'datasource-select':
      // 数据源来自「知识中心 · 数据库知识」,这里只引用 ID,不重复维护连接信息
      return (
        <div className="space-y-1.5">
          <select value={value || ''} onChange={(e) => onChange(e.target.value)}
                  className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none focus:border-blue-400">
            <option value="">请选择数据源…</option>
            {datasources.map((d) => (
              <option key={d.id} value={d.id}>{d.name}({d.db_type} · {d.scope === 'group' ? '集团级' : d.scenic_id})</option>
            ))}
          </select>
          {datasources.length === 0 && (
            <p className="text-[11px] text-amber-600">
              还没有可用数据源。请先到{' '}
              <button type="button" onClick={() => navigate('/knowledge/database/new')} className="font-medium underline">
                知识中心 · 数据库知识
              </button>{' '}新增并测试连接。
            </p>
          )}
        </div>
      );

    default:
      return <Input value={value ?? ''} onChange={(e) => onChange(e.target.value)} placeholder={f.placeholder}
                    className="font-mono text-sm" />;
  }
}

export default ToolNew;
