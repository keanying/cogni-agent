
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft, Settings2, Trash2, Lock, Unlock, Code2, ShieldCheck, Zap, Play, Clock, User, Loader2, Power,
  Globe, Database, FileCode, Boxes, ChevronDown, ShieldAlert, Eye, Layers, ChevronRight, BarChart3,
} from 'lucide-react';
import { Button } from '../../components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../../components/ui/card';
import { Badge } from '../../components/ui/badge';
import { useSearchParams } from 'react-router-dom';
import { useModalNavigate } from '../../lib/modalNav';
import { useInRouteModal } from '../../components/common/RouteModal';
import { maskSecret, maskSecretsDeep, looksLikeSecret } from '../../lib/maskSecret';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { CopyId } from '../../components/ui/PageHelpers';
import { cn } from '../../lib/utils';
import { confirmDialog } from '../../components/ui/confirm';

const TYPE_META: Record<string, { icon: any; badge: string; label: string }> = {
  http: { icon: Globe, badge: 'bg-blue-50 text-blue-600 border-blue-100', label: 'HTTP 接口' },
  db: { icon: Database, badge: 'bg-emerald-50 text-emerald-600 border-emerald-100', label: '数据库查询' },
  python: { icon: FileCode, badge: 'bg-amber-50 text-amber-600 border-amber-100', label: 'Python 插件' },
  mcp: { icon: Boxes, badge: 'bg-purple-50 text-purple-600 border-purple-100', label: 'MCP 工具' },
  quickbi: { icon: BarChart3, badge: 'bg-orange-50 text-orange-600 border-orange-100', label: 'Quick BI' },
};
const RISK_BADGE: Record<string, string> = {
  low: 'bg-emerald-50 text-emerald-600 border-emerald-100',
  medium: 'bg-amber-50 text-amber-600 border-amber-100',
  high: 'bg-rose-50 text-rose-600 border-rose-100',
};
const VIS_LABEL: Record<string, string> = { private: '仅创建者', scenic: '本景区', group: '集团内', public: '全平台' };
const SCHEMA_TYPE_LABEL: Record<string, string> = {
  string: '文本', integer: '整数', number: '数值', boolean: '布尔', array: '数组', object: '对象',
};

/** 版本状态元信息 */
const STATUS_META: Record<string, { label: string; cls: string }> = {
  published: { label: '已发布', cls: 'bg-emerald-50 text-emerald-600 border-emerald-100' },
  draft: { label: '草稿', cls: 'bg-amber-50 text-amber-600 border-amber-100' },
  offline: { label: '已下线', cls: 'bg-slate-100 text-slate-500 border-slate-200' },
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

/** 把 JSON Schema 的 properties 渲染成参数表,详情页第一眼就能看到"这个工具要什么入参"。 */
function SchemaParamTable({ schema }: { schema?: any }) {
  const props = schema?.properties || {};
  const required = new Set(schema?.required || []);
  const keys = Object.keys(props);
  if (keys.length === 0) return <p className="py-2 text-xs text-slate-400">未定义参数</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-100">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b bg-slate-50/70 text-left">
            <th className="px-3 py-2 font-semibold text-slate-500">参数</th>
            <th className="px-3 py-2 font-semibold text-slate-500">类型</th>
            <th className="px-3 py-2 font-semibold text-slate-500">必填</th>
            <th className="px-3 py-2 font-semibold text-slate-500">说明</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {keys.map((k) => {
            const p = props[k] || {};
            return (
              <tr key={k}>
                <td className="px-3 py-2 font-mono font-semibold text-slate-700">{k}</td>
                <td className="px-3 py-2"><Badge variant="outline" className="rounded-md text-[10px]">{SCHEMA_TYPE_LABEL[p.type] || p.type || '—'}</Badge></td>
                <td className="px-3 py-2 text-slate-500">{required.has(k) ? <span className="text-rose-500 font-medium">必填</span> : '选填'}</td>
                <td className="px-3 py-2 text-slate-500">{p.description || '—'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** 把 {{变量}} 渲染成高亮片段,不引入 HTML(纯 React 节点,安全)。 */
function HighlightVars({ text }: { text: string }) {
  const parts = text.split(/(\{\{[^}]+\}\})/g);
  return (
    <span>
      {parts.map((part, i) =>
        /^\{\{[^}]+\}\}$/.test(part)
          ? <span key={i} className="rounded bg-amber-50 px-1 font-semibold text-amber-600">{part}</span>
          : <span key={i}>{part}</span>
      )}
    </span>
  );
}


/** 响应字段说明(29 号):path/类型/单位/说明 表——执行链首调后附给大模型的同一份语义。 */
function ResponseFieldsBlock({ rs }: { rs: any }) {
  const fields = rs?.fields || [];
  if (fields.length === 0) return null;
  return (
    <div>
      <div className="mb-1.5 text-[11px] font-bold text-slate-500 uppercase">响应字段说明</div>
      <div className="overflow-hidden rounded-lg border border-slate-100">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="bg-slate-50 text-[10px] uppercase tracking-wide text-slate-400">
              <th className="px-3 py-1.5 font-bold">字段路径</th>
              <th className="px-3 py-1.5 font-bold">类型</th>
              <th className="px-3 py-1.5 font-bold">单位</th>
              <th className="px-3 py-1.5 font-bold">说明</th>
            </tr>
          </thead>
          <tbody>
            {fields.map((f: any, i: number) => (
              <tr key={i} className="border-t border-slate-50">
                <td className="px-3 py-1.5 font-mono text-[11px] text-slate-700">{f.path || f.name}</td>
                <td className="px-3 py-1.5 text-slate-500">{f.type || '—'}</td>
                <td className="px-3 py-1.5 text-slate-500">{f.unit || '—'}</td>
                <td className="px-3 py-1.5 text-slate-600">{f.desc || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rs.sample != null && (
        <pre className="mt-2 overflow-x-auto rounded-lg bg-slate-50 p-3 font-mono text-[11px] leading-relaxed text-slate-600">
          {typeof rs.sample === 'string' ? rs.sample : JSON.stringify(rs.sample, null, 2)}
        </pre>
      )}
    </div>
  );
}

/** 能力概览:按类型把 type_config 渲染成人类可读的形式,第一眼回答"这是什么、怎么用"。 */
function CapabilityOverview({ tool }: { tool: any }) {
  const cfg = tool.type_config || {};
  if (tool.type === 'http') {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge className="bg-slate-900 text-white border-none font-mono">{cfg.method || 'GET'}</Badge>
          <code className="break-all font-mono text-sm text-slate-700"><HighlightVars text={cfg.url || ''} /></code>
        </div>
        {cfg.headers && Object.keys(cfg.headers).length > 0 && (
          <div>
            <div className="mb-1.5 text-[11px] font-bold text-slate-500 uppercase">请求头</div>
            <div className="space-y-1 rounded-lg bg-slate-50 p-3">
              {Object.entries(cfg.headers).map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 font-mono text-xs">
                  <span className="text-sky-600">{k}</span>
                  <span className="text-slate-400">=</span>
                  {/* 明文密钥只露首尾(详情页是给人看的,不该把整把 key 摊在屏幕上);
                      并提示改成占位,由服务端注入 */}
                  <span className="text-slate-700 break-all"><HighlightVars text={maskSecret(k, String(v))} /></span>
                  {looksLikeSecret(k, String(v)) && (
                    <Badge variant="outline" className="ml-auto shrink-0 text-[9px] text-rose-600 border-rose-100 bg-rose-50"
                      title="请求头里写的是明文密钥。建议编辑工具改成 {{data_platform_key}} 占位,密钥在「系统设置 → 平台密钥」统一配置">
                      明文密钥 · 建议改为占位
                    </Badge>
                  )}
                  {String(v).includes('{{credential}}') && (
                    <Badge variant="outline" className="ml-auto shrink-0 text-[9px] text-amber-600 border-amber-100 bg-amber-50">凭证占位 · 加密注入</Badge>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
        <div>
          <div className="mb-1.5 text-[11px] font-bold text-slate-500 uppercase">输入参数</div>
          <SchemaParamTable schema={cfg.params_schema} />
        </div>
        <ResponseFieldsBlock rs={cfg.response_schema} />
        <div className="text-[11px] text-slate-400">
          允许运行时覆盖参数：{cfg.allow_override === false ? '关闭' : '开启'}
        </div>
      </div>
    );
  }
  if (tool.type === 'db') {
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2 text-xs">
          <span className="font-bold text-slate-500 uppercase">数据源</span>
          <code className="rounded bg-slate-50 px-2 py-0.5 font-mono text-slate-700">{cfg.datasource_id || '—'}</code>
        </div>
        <div>
          <div className="mb-1.5 text-[11px] font-bold text-slate-500 uppercase">SQL 模板</div>
          <pre className="overflow-x-auto rounded-lg bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-700 whitespace-pre-wrap break-all">
            <HighlightVars text={cfg.sql_template || ''} />
          </pre>
        </div>
        <ResponseFieldsBlock rs={cfg.response_schema} />
        <div className="text-[11px] text-slate-400">只读 SELECT，{"{{}}"} 变量由驱动参数化执行，天然免疫 SQL 注入。</div>
      </div>
    );
  }
  if (tool.type === 'python') {
    const lines = (cfg.script || '').split('\n').length;
    return (
      <div className="space-y-4">
        <div>
          <div className="mb-1.5 text-[11px] font-bold text-slate-500 uppercase">入参定义</div>
          <SchemaParamTable schema={cfg.input_schema} />
        </div>
        <div className="flex items-center gap-2 text-[11px] text-slate-400">
          <FileCode size={13} className="text-amber-500" />
          沙箱执行 · {lines} 行代码 · 超时 60s / 内存 512MB / 输出上限 1MB
        </div>
        <ResponseFieldsBlock rs={cfg.response_schema} />
      </div>
    );
  }
  if (tool.type === 'quickbi') {
    const CAP_LABEL: Record<string, string> = {
      dataset_qa: '问数 · 数据集', dashboard_qa: '报表 · 仪表板', file_qa: '问数 · 上传文件',
      insight: '解读', report: '报告', doc_parse: '文档解析', auto: '按意图自动路由',
    };
    const target = cfg.target || {};
    return (
      <div className="space-y-3 text-xs">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge className="bg-orange-500 text-white border-none">{CAP_LABEL[cfg.capability] || cfg.capability || 'dataset_qa'}</Badge>
          <span className="text-slate-500">连接</span>
          <code className="rounded bg-slate-50 px-2 py-0.5 font-mono text-slate-700">{cfg.connection_id || '默认连接'}</code>
        </div>
        {Object.keys(target).length > 0 && (
          <div className="flex flex-wrap gap-2">
            {Object.entries(target).map(([k, v]) => (
              <span key={k} className="rounded-lg border border-slate-100 bg-slate-50 px-2 py-1 font-mono text-slate-600">{k}={String(v)}</span>
            ))}
          </div>
        )}
        {cfg.fixed_question_id && <div className="text-slate-500">绑定固定问题:<code className="font-mono">{cfg.fixed_question_id}</code></div>}
        <div className="flex items-center gap-2 text-[11px] text-slate-400">
          <BarChart3 size={13} className="text-orange-500" />
          以连接的 user_token 身份调用 Quick BI OpenAPI · 可在「操练台」选 Quick BI 执行目标验证
        </div>
      </div>
    );
  }
  // mcp
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-xs">
        <span className="font-bold text-slate-500 uppercase">MCP Server</span>
        <code className="rounded bg-slate-50 px-2 py-0.5 font-mono text-slate-700">{cfg.server_id || '—'}</code>
        <span className="font-bold text-slate-500 uppercase ml-2">远端工具</span>
        <code className="rounded bg-slate-50 px-2 py-0.5 font-mono text-slate-700">{cfg.remote_tool_name || '—'}</code>
      </div>
      <div>
        <div className="mb-1.5 text-[11px] font-bold text-slate-500 uppercase">入参定义</div>
        <SchemaParamTable schema={cfg.input_schema} />
      </div>
    </div>
  );
}

const ToolDetail: React.FC = () => {
  const navigate = useModalNavigate();
  // 弹窗里:顶部固定,左侧版本列表 / 中间能力概览 / 右侧信息栏各自固定高度、各自滚动
  const inModal = useInRouteModal();
  const [params] = useSearchParams();
  const id = params.get('id');
  const [tool, setTool] = useState<any>(null);
  const [versions, setVersions] = useState<any[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [versionTick, setVersionTick] = useState(0);
  const [loading, setLoading] = useState(true);
  const [testParams, setTestParams] = useState('{}');
  const [testResult, setTestResult] = useState<string | null>(null);
  const [showConfig, setShowConfig] = useState(false);
  const [grants, setGrants] = useState<any[]>([]);
  const [grantKeyword, setGrantKeyword] = useState('');
  const [candidates, setCandidates] = useState<any[]>([]);
  const [searchingUsers, setSearchingUsers] = useState(false);
  const [showCandidates, setShowCandidates] = useState(false);
  const urlVersionConsumed = useRef(false);

  const loadGrants = () => {
    if (!id) return;
    api.listToolGrants(id).then((r) => setGrants(r.items || [])).catch(() => {});
  };
  useEffect(() => { if (id) loadGrants(); }, [id]);

  const load = () => {
    if (!id) { setLoading(false); return; }
    setLoading(true);
    api.getTool(id).then(setTool).catch((e: any) => toast.error(e.message || '加载失败')).finally(() => setLoading(false));
  };
  useEffect(load, [id]);

  // 版本列表(listResourceVersions 带 snapshot,是右侧「版本快照」视图的数据源):
  // URL version_id 仅进入页面时消费一次,其余刷新保留用户当前选中;无选中则默认第一个(最新)
  useEffect(() => {
    if (!id) return;
    api.listResourceVersions('tool', id)
      .then((r) => {
        const items = r.items || [];
        const sorted = sortVersions(items);
        setVersions(items);
        const urlV = urlVersionConsumed.current ? null : params.get('version_id');
        if (urlV && items.some((v: any) => v.id === urlV)) urlVersionConsumed.current = true;
        setSelectedVersionId((prev) => {
          if (urlV && items.some((v: any) => v.id === urlV)) return urlV;
          if (prev && items.some((v: any) => v.id === prev)) return prev;
          return sorted[0]?.id ?? null;
        });
      })
      .catch(() => {});
  }, [id, versionTick]);

  const viewingVersion = useMemo(
    () => versions.find((v: any) => v.id === selectedVersionId) || null,
    [versions, selectedVersionId],
  );

  // 快照优先:选中版本后,description / type / type_config / risk_level / visibility 均以快照为准;
  // 无版本记录(未发布)时回退主表当前内容
  const viewTool = useMemo(() => {
    if (!tool) return tool;
    if (!viewingVersion?.snapshot) return tool;
    const snap = viewingVersion.snapshot;
    const out: any = { ...tool };
    (['description', 'type', 'type_config', 'risk_level', 'visibility'] as const).forEach((k) => {
      if (snap[k] !== undefined) out[k] = snap[k];
    });
    return out;
  }, [tool, viewingVersion]);

  const publishedCount = versions.filter((v: any) => v.status === 'published').length;
  const sortedVersions = useMemo(() => sortVersions(versions), [versions]);
  const TypeIcon = TYPE_META[viewTool?.type]?.icon || Globe;
  const viewMeta = viewingVersion ? STATUS_META[viewingVersion.status] || STATUS_META.offline : null;
  const viewLabel = viewingVersion
    ? (viewingVersion.status === 'draft' ? '草稿' : `v${viewingVersion.version} ${viewMeta?.label || ''}`)
    : publishedCount > 0
      ? `已发布 ${publishedCount} 版`
      : '未发布';

  const handleToggleLock = async () => { try { await api.lockTool(id!); toast.success('锁定状态已更新'); load(); } catch (e: any) { toast.error(e.message); } };
  const handleDelete = async () => {
    if (!await confirmDialog({ title: '删除该工具',
      message: '绑定了它的 Skill/Agent 在执行时会少掉这个工具,已发布版本的快照不受影响。',
      confirmLabel: '删除', danger: true })) return;
    try { await api.deleteTool(id!); toast.success('已删除'); navigate('/tools/tool'); } catch (e: any) { toast.error(e.message); }
  };
  // D1(13 号文档):草稿行「发布版本」—— 调 POST /versions/{id}/publish(评测门禁仅 agent,
  // tool 无门禁;发布预检会校验无版本依赖)。15 号文档:无默认版本概念,发布仅生成 published
  // 快照不再物化主表;发布返回 D7-A hint 时用黄条提示。
  const [publishingId, setPublishingId] = useState<string | null>(null);
  const handlePublishVersion = async (versionId: string) => {
    setPublishingId(versionId);
    try {
      const r: any = await api.publishResourceVersion(versionId);
      if (r.ok === false) { toast.error(r.message || r.hint || '发布被阻断'); return; }
      toast.success(r.version != null ? `已发布 v${r.version}` : '发布成功');
      if (r.hint) toast.warning(r.hint);
      setVersionTick((t) => t + 1);
    } catch (e: any) { toast.error(e.message || '发布失败'); }
    finally { setPublishingId(null); }
  };
  // 15 号文档:无默认版本概念,任意已发布版本均可下线;后端对「被更高版本/上层资源引用」
  // 的版本会拒绝下线,前端直接展示后端错误即可。
  const [offliningId, setOffliningId] = useState<string | null>(null);
  const handleOfflineVersion = async (versionId: string) => {
    if (!await confirmDialog({ title: '下线该版本',
      message: '版本快照保留在历史里,但不再作为可用版本被解析 —— 没有其他已发布版本时,这个工具将无法被调用。',
      confirmLabel: '下线', danger: true })) return;
    setOffliningId(versionId);
    try {
      const r: any = await api.offlineResourceVersion(versionId);
      toast.success(r?.message || '已下线');
      setVersionTick((t) => t + 1);
    } catch (e: any) { toast.error(e.message || '下线失败'); }
    finally { setOffliningId(null); }
  };
  const handleTest = async () => {
    let p: any;
    try { p = JSON.parse(testParams); } catch { toast.error('参数需要是合法 JSON'); return; }
    try {
      const r = await api.testTool(id!, p);
      setTestResult(`已创建任务 ${r.task_id.slice(0, 8)}...，可在「审计中心 · 任务中心」查看真实执行结果`);
    } catch (e: any) { toast.error(e.message || '调试失败'); }
  };
  const searchUsers = async (kw: string) => {
    setGrantKeyword(kw);
    if (!kw.trim()) { setCandidates([]); setShowCandidates(false); return; }
    setSearchingUsers(true);
    try {
      const granted = new Set(grants.filter((g) => g.grantee_type === 'user').map((g) => g.grantee_id));
      const r = await api.searchToolUsers(kw.trim());
      setCandidates((r.items || []).filter((u: any) => !granted.has(u.id)));
      setShowCandidates(true);
    } catch (e: any) { toast.error(e.message || '搜索用户失败'); }
    finally { setSearchingUsers(false); }
  };
  const handleGrantUser = async (u: any) => {
    try {
      await api.shareTool(id!, { grantee_type: 'user', grantee_id: u.id, permission: 'use' });
      toast.success(`已授权 ${u.name || u.phone || u.account}${u.phone ? `（${u.phone}）` : ''}`);
      setGrantKeyword(''); setCandidates([]); setShowCandidates(false);
      loadGrants();
    } catch (e: any) { toast.error(e.message || '授权失败'); }
  };
  const handleRevokeGrant = async (g: any) => {
    if (!await confirmDialog({ title: `移除 ${g.grantee_name || g.grantee_id} 的授权`,
      message: '移除后对方将不能再使用这个工具(除非他本来就在可见范围内)。',
      confirmLabel: '移除', danger: true })) return;
    try { await api.deleteToolGrant(id!, g.grant_id); toast.success('已移除授权'); loadGrants(); }
    catch (e: any) { toast.error(e.message || '移除失败'); }
  };

  if (loading) return <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>;
  if (!tool) return <div className="text-center py-20 text-slate-400">未找到该工具</div>;

  return (
    <div className={cn('max-w-[1500px] mx-auto animate-in fade-in duration-500',
      inModal ? 'space-y-4 lg:space-y-0 lg:h-full lg:flex lg:flex-col lg:gap-4' : 'space-y-6')}>
      {/* ── 头部(弹窗里固定在顶部,不随内容滚走) ── */}
      <div className={cn(inModal ? 'shrink-0 space-y-3' : 'space-y-6')}>
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => navigate('/tools/tool')} className="rounded-full">
            <ArrowLeft className="h-5 w-5" />
          </Button>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-bold text-slate-900 tracking-tight">{viewTool.name}</h1>
              <CopyId id={tool.id} showFull className="text-[11px]" />
              <Badge className={viewingVersion
                ? viewMeta?.cls
                : publishedCount > 0
                  ? 'bg-emerald-50 text-emerald-600 border-emerald-100'
                  : 'bg-slate-100 text-slate-500 border-slate-200'}>
                {viewLabel}
              </Badge>
              {tool.locked && <Lock size={16} className="text-amber-500" />}
            </div>
            <p className="text-slate-500 text-sm">{viewTool.description}</p>
          </div>
        </div>
        <div className="flex gap-3">
          <Button variant="outline" className="rounded-xl h-10 gap-2 font-medium" onClick={() => navigate(`/tools/tool/edit?id=${id}`)}>
            <Settings2 size={16} /> 编辑工具
          </Button>
          <Button className="bg-slate-900 hover:bg-black text-white rounded-xl h-10 px-6 font-bold shadow-lg shadow-slate-200 gap-2" onClick={handleToggleLock}>
            {tool.locked ? <Unlock size={15} /> : <Lock size={15} />} {tool.locked ? '解锁工具' : '锁定工具'}
          </Button>
        </div>
      </div>

      {/* ── 状态条:基于当前查看的版本快照 ── */}
      <div className="flex flex-wrap items-center gap-2">
        <Badge className={`gap-1.5 border ${TYPE_META[viewTool.type]?.badge}`}><TypeIcon size={12} />{TYPE_META[viewTool.type]?.label || viewTool.type}</Badge>
        <Badge className={`border uppercase ${RISK_BADGE[viewTool.risk_level] || ''}`}>{viewTool.risk_level}</Badge>
        {viewTool.risk_level === 'high' && (
          <span className="flex items-center gap-1 text-[11px] font-medium text-rose-500"><ShieldAlert size={12} />每次调用需人工审批</span>
        )}
        <Badge className={tool.active ? 'bg-emerald-50 text-emerald-600 border-emerald-100 gap-1' : 'bg-slate-100 text-slate-500 border-slate-200 gap-1'}>
          <Power size={11} />{tool.active ? '启用' : '禁用'}
        </Badge>
        <Badge variant="outline" className="text-slate-600">{VIS_LABEL[viewTool.visibility] || viewTool.visibility}</Badge>
        {tool.locked && <Badge variant="outline" className="gap-1 border-amber-100 bg-amber-50 text-amber-600"><Lock size={11} />已锁定</Badge>}
      </div>

      {/* ── 正在查看的版本视角 ── */}
      {viewingVersion && (
        <div className="flex items-center gap-2 flex-wrap rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-xs text-slate-500">
          <span>正在查看</span>
          <Badge className={viewMeta?.cls}>{viewLabel}</Badge>
          <span className="text-slate-400">
            · {viewingVersion.published_by_name || viewingVersion.published_by || '系统'}
            {' '}发布于 {viewingVersion.created_at ? new Date(viewingVersion.created_at).toLocaleString() : '—'}
            {' '}· 切换左侧版本可浏览历史快照；在线调试 / 授权以工具当前配置为准
          </span>
        </div>
      )}
      </div>

      <div className={cn('grid grid-cols-1 lg:grid-cols-12 gap-6',
        inModal ? 'lg:flex-1 lg:min-h-0 lg:grid-rows-[minmax(0,1fr)]' : 'items-start')}>
        {/* ── 左侧版本列表 ── */}
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
                  <li className="px-4 py-10 text-center text-xs text-slate-300">暂无版本记录：编辑保存为草稿并发布后，历史版本将展示于此</li>
                )}
              </ul>
            </CardContent>
          </Card>
        </div>

        {/* ── 右侧详情 ── */}
        <div className={cn('lg:col-span-9 grid grid-cols-1 lg:grid-cols-12 gap-6',
          inModal && 'lg:h-full lg:min-h-0 lg:grid-rows-[minmax(0,1fr)]')}>
          <div className={cn('lg:col-span-8 space-y-6', inModal && 'lg:h-full lg:overflow-y-auto custom-scrollbar lg:pr-1')}>
            {/* 能力概览:第一眼回答「这是什么、怎么用」(选中版本快照) */}
            <Card className="border-slate-200 shadow-sm">
              <CardHeader className="border-b bg-slate-50/50 py-4">
                <CardTitle className="text-sm font-bold flex items-center gap-2"><Eye size={16} className="text-blue-500" /> 能力概览</CardTitle>
              </CardHeader>
              <CardContent className="p-6">
                <CapabilityOverview tool={viewTool} />
              </CardContent>
            </Card>

            {/* 实现细节:type_config 原文,默认折叠,排障时才展开 */}
            <Card className="border-slate-200 shadow-sm">
              <button type="button" onClick={() => setShowConfig(!showConfig)}
                className="w-full flex items-center justify-between border-b bg-slate-50/50 py-4 px-6 hover:bg-slate-50 transition-colors">
                <span className="text-sm font-bold flex items-center gap-2"><Code2 size={16} className="text-slate-500" /> 实现细节（type_config）</span>
                <ChevronDown size={16} className={`text-slate-400 transition-transform ${showConfig ? 'rotate-180' : ''}`} />
              </button>
              {showConfig && (
                <CardContent className="p-6">
                  <div className="bg-slate-950 rounded-xl p-4 overflow-x-auto">
                    <pre className="text-emerald-400 font-mono text-xs leading-relaxed">{JSON.stringify(maskSecretsDeep(viewTool.type_config), null, 2)}</pre>
                  </div>
                </CardContent>
              )}
            </Card>

            {/* 在线调试(以工具当前配置真实调用) */}
            <Card className="border-slate-200 shadow-sm">
              <CardHeader className="border-b bg-slate-50/50 py-4">
                <CardTitle className="text-sm font-bold flex items-center gap-2"><Play size={16} className="text-amber-500" /> 在线调试</CardTitle>
              </CardHeader>
              <CardContent className="p-6 space-y-3">
                <textarea value={testParams} onChange={(e) => setTestParams(e.target.value)} rows={4}
                  className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs font-mono outline-none" />
                <Button onClick={handleTest} className="bg-[#1677ff] hover:bg-[#4096ff] gap-2"><Zap size={14} />发起真实调用</Button>
                {testResult && <div className="text-xs bg-blue-50 text-blue-600 rounded-lg p-3">{testResult}</div>}
              </CardContent>
            </Card>

            {/* 版本历史(管理与发布入口;行点击可切换查看) */}
            <Card className="border-slate-200 shadow-sm">
              <CardHeader className="border-b py-4">
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <CardTitle className="text-sm font-bold">版本历史</CardTitle>
                  {publishedCount === 0 && (
                    <span className="text-[11px] font-medium text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-2.5 py-1">
                      当前无已发布版本：编辑保存为草稿后，点「发布」方可被 Skill / Agent 引用
                    </span>
                  )}
                </div>
              </CardHeader>
              <CardContent className="p-0">
                <table className="w-full text-sm">
                  <thead><tr className="text-left border-b bg-slate-50">
                    <th className="p-4 font-semibold text-slate-600">版本</th>
                    <th className="p-4 font-semibold text-slate-600">发布人</th>
                    <th className="p-4 font-semibold text-slate-600">发布时间</th>
                    <th className="p-4 font-semibold text-slate-600">状态</th>
                    <th className="p-4 font-semibold text-slate-600">操作</th>
                  </tr></thead>
                  <tbody className="divide-y">
                    {sortedVersions.map((row: any, i: number) => {
                      const active = row.id === viewingVersion?.id;
                      return (
                        <tr key={row.id || i}
                          onClick={() => setSelectedVersionId(row.id)}
                          title="点击查看该版本快照"
                          className={cn('cursor-pointer transition-colors', active ? 'bg-blue-50/50' : 'hover:bg-slate-50/50')}>
                          <td className="p-4">
                            {row.status === 'draft' ? (
                              <Badge variant="outline" className="rounded-md bg-amber-50 text-amber-600 border-amber-100">草稿</Badge>
                            ) : (
                              <Badge variant="outline" className="rounded-md border-blue-100 bg-blue-50 text-blue-600">{row.version}</Badge>
                            )}
                          </td>
                          <td className="p-4 text-slate-700">{row.published_by_name ? `${row.published_by_name}${row.published_by_phone ? `（${row.published_by_phone}）` : ''}` : row.published_by}</td>
                          <td className="p-4 text-slate-500 font-mono text-xs">{row.created_at ? new Date(row.created_at).toLocaleString() : '—'}</td>
                          <td className="p-4">
                            <Badge variant="outline" className={row.status === 'published' ? 'rounded-md bg-emerald-50 text-emerald-600 border-emerald-100'
                              : row.status === 'draft' ? 'rounded-md bg-amber-50 text-amber-600 border-amber-100'
                                : 'rounded-md bg-slate-100 text-slate-400 border-slate-200'}>
                              {row.status === 'published' ? '已发布' : row.status === 'draft' ? '草稿' : '已下线'}
                            </Badge>
                          </td>
                          <td className="p-4">
                            {row.status === 'draft' ? (
                              <Button size="sm" disabled={publishingId === row.id}
                                className="h-7 gap-1 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-xs font-medium px-3"
                                onClick={(e) => { e.stopPropagation(); handlePublishVersion(row.id); }}>
                                <Play size={11} /> {publishingId === row.id ? '发布中…' : '发布'}
                              </Button>
                            ) : row.status === 'published' ? (
                              <Button size="sm" variant="ghost" disabled={offliningId === row.id}
                                className="h-7 rounded-lg text-slate-500 hover:text-rose-600 text-xs font-medium px-3"
                                onClick={(e) => { e.stopPropagation(); handleOfflineVersion(row.id); }}>
                                {offliningId === row.id ? '下线中…' : '下线'}
                              </Button>
                            ) : null}
                          </td>
                        </tr>
                      );
                    })}
                    {sortedVersions.length === 0 && (
                      <tr><td colSpan={5} className="p-6 text-center text-slate-300">暂无历史版本：编辑工具保存后生成草稿，发布后展示于此</td></tr>
                    )}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          </div>

          {/* 右侧信息栏 */}
          <div className={cn('lg:col-span-4 space-y-6', inModal && 'lg:h-full lg:overflow-y-auto custom-scrollbar lg:pr-1')}>
            <Card className="border-slate-200 shadow-sm overflow-hidden">
              <CardHeader className="border-b bg-slate-50/50 py-4"><CardTitle className="text-sm font-bold">基础信息</CardTitle></CardHeader>
              <CardContent className="p-5 space-y-5">
                <div className="flex items-center justify-between"><span className="text-xs text-slate-500">所属景区</span><span className="text-xs font-bold text-slate-700">{tool.scenic_id || '集团级'}</span></div>
                <div className="flex items-center gap-3">
                  <div className="h-8 w-8 rounded-lg bg-slate-100 flex items-center justify-center shrink-0"><User size={14} className="text-slate-500" /></div>
                  <div><div className="text-[10px] text-slate-400 font-bold uppercase">创建者</div><div className="text-xs font-bold text-slate-700">{tool.owner_name ? `${tool.owner_name}${tool.owner_phone ? `（${tool.owner_phone}）` : ''}` : tool.owner_user_id}</div></div>
                </div>
                <div className="flex items-center gap-3">
                  <div className="h-8 w-8 rounded-lg bg-slate-100 flex items-center justify-center shrink-0"><Clock size={14} className="text-slate-500" /></div>
                  <div><div className="text-[10px] text-slate-400 font-bold uppercase">创建时间</div><div className="text-xs font-bold text-slate-700">{new Date(tool.created_at).toLocaleString()}</div></div>
                </div>
              </CardContent>
            </Card>

            <Card className="border-slate-200 shadow-sm">
              <CardHeader className="border-b bg-slate-50/50 py-4">
                <CardTitle className="text-sm font-bold flex items-center gap-2"><ShieldCheck size={16} className="text-emerald-500" /> 共享授权</CardTitle>
              </CardHeader>
              <CardContent className="p-5 space-y-3">
                <div className="relative">
                  <div className="flex gap-2">
                    <input value={grantKeyword} onChange={(e) => searchUsers(e.target.value)}
                      onFocus={() => { if (grantKeyword.trim()) setShowCandidates(true); }}
                      onBlur={() => setTimeout(() => setShowCandidates(false), 150)}
                      placeholder="搜索姓名 / 账号 / 手机号，选中即授权…"
                      className="flex-1 h-9 px-3 rounded-lg border border-slate-200 text-xs outline-none" />
                    {searchingUsers && <Loader2 className="animate-spin text-slate-400 self-center" size={14} />}
                  </div>
                  {showCandidates && candidates.length > 0 && (
                    <ul className="absolute z-10 mt-1 w-full max-h-48 overflow-auto rounded-lg border border-slate-200 bg-white shadow-lg">
                      {candidates.map((u) => (
                        <li key={u.id} onMouseDown={() => handleGrantUser(u)}
                          className="px-3 py-2 hover:bg-blue-50 cursor-pointer flex items-center justify-between gap-2">
                          <div className="min-w-0">
                            <div className="text-xs font-bold text-slate-700 truncate">{u.name || '-'}</div>
                            <div className="text-[10px] text-slate-400 truncate">{u.phone || u.account}</div>
                          </div>
                          <span className="text-[10px] text-blue-500 shrink-0">授权</span>
                        </li>
                      ))}
                    </ul>
                  )}
                  {showCandidates && candidates.length === 0 && grantKeyword.trim() && !searchingUsers && (
                    <p className="absolute z-10 mt-1 w-full rounded-lg border border-slate-200 bg-white p-3 text-[11px] text-slate-400 shadow-lg">未找到可授权的用户（可能已授权或无匹配）</p>
                  )}
                </div>
                <div className="space-y-2 border-t border-slate-100 pt-3">
                  {grants.length === 0 ? (
                    <p className="text-[11px] text-slate-400">暂无定向授权。通过上方搜索为指定用户授权。</p>
                  ) : (
                    grants.map((g) => (
                      <div key={g.grant_id} className="flex items-center justify-between rounded-lg border border-slate-100 bg-slate-50/50 px-3 py-2">
                        <div className="min-w-0">
                          <div className="text-xs font-bold text-slate-700 truncate">{g.grantee_type === 'user' ? (g.grantee_name || g.grantee_id) : `景区 ${g.grantee_id}`}</div>
                          <div className="text-[10px] text-slate-400">{g.grantee_type === 'user' ? g.grantee_phone : 'scenic_all'} · {g.permission}</div>
                        </div>
                        <button onClick={() => handleRevokeGrant(g)} title="移除授权"
                          className="text-rose-400 hover:text-rose-600 transition-colors shrink-0">
                          <Trash2 size={14} />
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </CardContent>
            </Card>

            <Button variant="ghost" className="w-full text-rose-500 hover:text-rose-600 hover:bg-rose-50 rounded-xl h-11 gap-2 font-bold transition-colors" onClick={handleDelete}>
              <Trash2 size={16} /> 删除该工具
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ToolDetail;
