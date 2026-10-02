
import React, { useEffect, useState } from 'react';
import {
  Plus, Search, Cpu, Database, FileCode, Lock, Unlock, ExternalLink,
  Settings2, Trash2, Loader2, Play, BarChart3, FileText, Sparkles, ChevronDown, RefreshCw,
} from 'lucide-react';
import { CopyId, Modal } from '../../components/ui/PageHelpers';
import { Card, CardContent } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';
import { useLocation, useNavigate } from 'react-router-dom';
import { openInModal } from '../../lib/modalNav';
import { CARD_GRID, CardMeta } from '../../components/common/CardMeta';
import * as api from '../../lib/api';
import { ConfirmDialog } from '../../components/ui/ConfirmDialog';
import { ApiDocImportModal } from './ApiDocImportModal';
import { canEditRow, useCapabilities } from '../../lib/capabilities';

/**
 * 系统内置工具:免配置、免发版、对所有技能默认可用。
 *
 * 为什么要在页面上露出来 —— 它们不落库、不走版本,页面上原本一个都看不见,
 * 于是运营会照着自己的需要再建一份同名工具(「数值计算」这种),填参数、发版本、再分发,
 * 而系统里本来就有一个更稳的。更糟的是自建那份每改一次都要重新发版,忘了发版就是
 * 「改了没生效」,而且不报错。
 *
 * 默认折叠:它不是每天要看的东西,但要找得到。
 */
const BuiltinTools: React.FC = () => {
  const [items, setItems] = useState<api.BuiltinTool[]>([]);
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!open || loaded) return;
    api.listBuiltinTools()
      .then((r) => { setItems(r.items); setLoaded(true); })
      .catch(() => setLoaded(true));
  }, [open, loaded]);

  const groups = React.useMemo(() => {
    const m = new Map<string, api.BuiltinTool[]>();
    items.forEach((t) => m.set(t.group, [...(m.get(t.group) || []), t]));
    return Array.from(m.entries());
  }, [items]);

  return (
    <Card className="border-slate-200 shadow-sm bg-white overflow-hidden">
      <button type="button" onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-3 px-5 py-4 text-left hover:bg-slate-50/70 transition-colors">
        <div className="h-9 w-9 rounded-xl bg-violet-50 border border-violet-100 flex items-center justify-center">
          <Sparkles size={17} className="text-violet-500" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="font-semibold text-slate-800 text-sm">
            系统内置工具
            {loaded && <span className="ml-2 text-xs font-normal text-slate-400">{items.length} 个</span>}
          </div>
          <div className="text-xs text-slate-500 mt-0.5">
            免配置、免发版，所有技能默认可用，且始终是最新实现 —— 这些能力不必再自己建一份
          </div>
        </div>
        <ChevronDown size={16} className={cn('text-slate-400 transition-transform', open && 'rotate-180')} />
      </button>

      {open && (
        <div className="border-t border-slate-100 px-5 py-4 space-y-5">
          {!loaded && <div className="flex justify-center py-6"><Loader2 className="animate-spin text-slate-300" size={20} /></div>}
          {loaded && items.length === 0 && (
            <div className="text-center py-6 text-xs text-slate-400">读不到内置工具清单</div>
          )}
          {groups.map(([group, list]) => (
            <div key={group}>
              <div className="text-[11px] font-semibold text-slate-400 mb-2 tracking-wide">{group}</div>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
                {list.map((t) => (
                  <div key={t.name} className="rounded-xl border border-slate-100 bg-slate-50/60 px-3 py-2.5">
                    <div className="flex items-center gap-2 mb-1">
                      <code className="text-[12px] font-semibold text-violet-700">{t.name}</code>
                      <Badge className="border border-emerald-200 bg-emerald-50 text-emerald-600 text-[10px] px-1.5 py-0">
                        默认在线
                      </Badge>
                    </div>
                    <div className="text-[11px] leading-relaxed text-slate-500">{t.description}</div>
                    {t.parameters.length > 0 && (
                      <div className="mt-1.5 text-[10px] text-slate-400">
                        参数：{t.parameters.map((p) => (
                          <span key={p} className={cn('mr-1.5', t.required.includes(p) && 'text-slate-600 font-medium')}>
                            {p}{t.required.includes(p) ? '*' : ''}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
          <p className="text-[11px] text-slate-400 leading-relaxed">
            这些工具不可编辑，也不需要编辑：它们随版本升级自动更新，不存在「改了没发版」的问题。
            取数工具返回上千行时，明细会留在服务端，只把数据摘要和一个句柄给模型 ——
            模型再用上面的数据分析工具按句柄去算，明细一行都不进上下文，也就不会被截断。
          </p>
        </div>
      )}
    </Card>
  );
};

const ToolList: React.FC = () => {
  const [tools, setTools] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<any>(null);
  const [deleting, setDeleting] = useState(false);
  const [apiDocOpen, setApiDocOpen] = useState(false);
  const [syncOpen, setSyncOpen] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [refreshExisting, setRefreshExisting] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  // 数据接口工具全平台共用(visibility=public),只有集团角色能同步 —— 与后端同口径
  const cap = useCapabilities();
  const canSyncDataApi = canEditRow(cap, null);

  // 数据指标接口文档 → HTTP 工具。服务启动时已自动补过;这里用于删错了补回、或按文档刷新已有的。
  const runSync = async () => {
    setSyncing(true);
    try {
      const r = await api.syncDataApiTools(refreshExisting);
      toast.success(r.message);
      if (r.errors.length) toast.warning(r.errors.join('\n'));
      if (!r.key_configured) toast.warning('数据平台密钥未配置(DATA_PLATFORM_XPFTKEY),这些工具调用时会报「密钥未配置」');
      setSyncOpen(false);
      load(keyword);
    } catch (err: any) { toast.error(err.message || '同步失败'); }
    finally { setSyncing(false); }
  };

  const load = async (kw?: string) => {
    setLoading(true);
    try { setTools((await api.listTools(kw)).items); }
    catch (e: any) { toast.error(e.message || '加载工具列表失败'); }
    finally { setLoading(false); }
  };
  // 查看/修改在弹窗里做;弹窗关闭(跳回列表)时 location.key 变化 → 重新拉一次,改完立刻看得到
  useEffect(() => { load(keyword); }, [location.key]);

  const toggleStatus = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try { await api.toggleTool(id); toast.success('状态已更新'); load(keyword); }
    catch (err: any) { toast.error(err.message || '操作失败'); }
  };

  const toggleLock = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try { await api.lockTool(id); toast.success('锁定状态已更新'); load(keyword); }
    catch (err: any) { toast.error(err.message || '操作失败'); }
  };

  const confirmDelete = async (force?: boolean) => {
    if (!deleteTarget) return;
    setDeleting(true);
    try { await api.deleteTool(deleteTarget.id, !!force); toast.success(force ? '已强制删除并归档' : '已删除'); setDeleteTarget(null); load(keyword); }
    catch (err: any) { toast.error(err.message || '删除失败'); }
    finally { setDeleting(false); }
  };

  const getIcon = (type: string) => {
    switch (type) {
      case 'http': return <ExternalLink size={15} className="text-blue-500" />;
      case 'db': return <Database size={15} className="text-emerald-500" />;
      case 'python': return <FileCode size={15} className="text-amber-500" />;
      case 'quickbi': return <BarChart3 size={15} className="text-orange-500" />;
      default: return <Cpu size={15} className="text-slate-500" />;
    }
  };

  const getSecurityColor = (level: string) => {
    switch (level) {
      case 'low': return 'bg-emerald-50 text-emerald-600 border-emerald-100';
      case 'medium': return 'bg-amber-50 text-amber-600 border-amber-100';
      case 'high': return 'bg-rose-50 text-rose-600 border-rose-100';
      default: return 'bg-slate-50 text-slate-600 border-slate-100';
    }
  };

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">Tool 中心</h1>
          <p className="text-slate-500 text-sm mt-1">管理系统所有的基础原子能力，可被 Skill 和 Agent 引用。</p>
        </div>
        <div className="flex gap-3">
          {/* 按接口文档新建:运维手上现成的就是一份接口文档,让人再手工翻译成 JSON Schema
              既慢又容易译错 —— 译错的表现还特别隐蔽(模型照着错 schema 传参,看起来像接口有问题) */}
          {canSyncDataApi && (
            <Button variant="outline" onClick={() => setSyncOpen(true)}
              className="h-11 px-5 rounded-xl border-slate-200 bg-white gap-2 text-slate-600 font-medium">
              <RefreshCw size={18} />
              同步数据接口工具
            </Button>
          )}
          <Button variant="outline" onClick={() => setApiDocOpen(true)}
            className="h-11 px-5 rounded-xl border-slate-200 bg-white gap-2 text-slate-600 font-medium">
            <FileText size={18} />
            按接口文档新建
          </Button>
          <Button onClick={() => navigate('/tools/tool/new')} className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl gap-2 shadow-lg shadow-blue-200 h-11 px-6">
            <Plus size={18} />
            新建 Tool
          </Button>
        </div>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/50 backdrop-blur-sm">
        <CardContent className="p-4">
          <div className="flex flex-col md:flex-row gap-4">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
              <Input value={keyword} onChange={(e) => setKeyword(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && load(keyword)}
                placeholder="搜索工具名称，回车搜索..." className="pl-10 h-11 bg-white border-slate-200 rounded-xl focus-visible:ring-[#2563eb]" />
            </div>
            <Button variant="outline" className="h-11 rounded-xl border-slate-200 bg-white gap-2 text-slate-600 font-medium" onClick={() => load(keyword)}>
              搜索
            </Button>
          </div>
        </CardContent>
      </Card>

      <BuiltinTools />

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>
      ) : tools.length === 0 ? (
        <div className="text-center py-20 text-slate-300 text-sm">暂无工具，点击右上角新建</div>
      ) : (
        <div className={CARD_GRID}>
          {tools.map((tool) => (
            <Card
              key={tool.id}
              onClick={() => openInModal(navigate, location, `/tools/tool/detail?id=${tool.id}`)}
              className={cn(
                'group border-slate-200 shadow-sm hover:shadow-md hover:border-blue-200 transition-all bg-white overflow-hidden flex flex-col cursor-pointer',
                !tool.active && 'opacity-75'
              )}
            >
              <div className="p-4 flex-1">
                <div className="flex items-start gap-2.5">
                  <div className="h-8 w-8 shrink-0 rounded-lg bg-slate-50 border border-slate-100 flex items-center justify-center">
                    {getIcon(tool.type)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <h3 className="text-sm font-bold text-slate-900 group-hover:text-blue-600 transition-colors truncate" title={tool.name}>{tool.name}</h3>
                    <CopyId id={tool.id} />
                  </div>
                  {tool.locked && <Lock size={13} className="mt-1 shrink-0 text-amber-500" />}
                  <button
                    onClick={(e) => toggleStatus(tool.id, e)}
                    title="点击切换启用 / 停用"
                    className={cn(
                      'shrink-0 h-6 px-1.5 rounded-md text-[10px] font-bold transition-colors',
                      tool.active ? 'bg-emerald-50 text-emerald-600' : 'bg-slate-100 text-slate-400'
                    )}
                  >
                    {tool.active ? '启用' : '停用'}
                  </button>
                </div>

                <p className="mt-2 text-xs text-slate-500 line-clamp-2" title={tool.description}>{tool.description || '—'}</p>

                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  <Badge variant="outline" className="rounded-md border-slate-100 bg-slate-50 text-slate-500 font-medium text-[10px] px-1.5 py-0">
                    {tool.type}
                  </Badge>
                  <Badge variant="outline" className={cn('rounded-md font-medium text-[10px] px-1.5 py-0', getSecurityColor(tool.risk_level))}>
                    {String(tool.risk_level || '').toUpperCase()} Risk
                  </Badge>
                  {/* 15 号文档:无默认版本概念(主表 version 亦不再随发布写入),发布态按后端
                      published_count(已发布版本数)推导,不再信主表 status/default_version */}
                  <Badge variant="outline" className={cn('rounded-md font-medium text-[10px] px-1.5 py-0', (tool.published_count ?? 0) > 0
                    ? 'border-emerald-100 bg-emerald-50 text-emerald-600'
                    : 'border-amber-100 bg-amber-50 text-amber-600')}>
                    {(tool.published_count ?? 0) > 0 ? `已发布 ${tool.published_count} 版` : '未发布'}
                  </Badge>
                  {tool.superseded_by && (
                    <Badge variant="outline"
                           className="rounded-md border-orange-200 bg-orange-50 text-orange-600 font-medium text-[10px] px-1.5 py-0">
                      已被内置 {tool.superseded_by} 取代
                    </Badge>
                  )}
                </div>
                {/* 这一行从建出来那天起就调不通(type=builtin 是按**工具名**去内置注册表
                    找函数,中文名一个都找不到),而它看起来和别的工具一模一样、还挂着绿色的
                    「启用」。不把话说在卡片上,运维没有任何线索知道该下线它。 */}
                {tool.superseded_note && (
                  <p className="mt-2 rounded-lg border border-orange-100 bg-orange-50/60 px-2 py-1
                                text-[11px] leading-relaxed text-orange-700">
                    {tool.superseded_note}
                  </p>
                )}
              </div>
              <div className="px-4 py-2 bg-slate-50/50 border-t flex items-center justify-between gap-2">
                <CardMeta item={tool} />
                <div className="flex shrink-0 gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg" title={tool.locked ? '解锁' : '锁定'} onClick={(e) => toggleLock(tool.id, e)}>
                    {tool.locked ? <Unlock size={14} className="text-slate-500" /> : <Lock size={14} className="text-slate-500" />}
                  </Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg" title="修改" onClick={(e) => { e.stopPropagation(); openInModal(navigate, location, `/tools/tool/edit?id=${tool.id}`); }}>
                    <Settings2 size={14} className="text-slate-500" />
                  </Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg text-rose-500" title="删除" onClick={(e) => { e.stopPropagation(); setDeleteTarget(tool); }}>
                    <Trash2 size={14} />
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {syncOpen && (
        <Modal title="同步数据接口工具" onClose={() => !syncing && setSyncOpen(false)}
          footer={<>
            <Button variant="outline" disabled={syncing} onClick={() => setSyncOpen(false)}>取消</Button>
            <Button disabled={syncing} onClick={runSync} className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white gap-2">
              {syncing && <Loader2 size={16} className="animate-spin" />}开始同步
            </Button>
          </>}>
          <p className="text-sm text-slate-600 leading-relaxed">
            按随系统打包的《数据指标接口文档》(30 个接口)建工具:工具库里<b>没有的新建</b>,
            已有的按接口地址识别、默认不动。服务启动时已经自动做过一次,这里用于删错了补回,或文档更新后刷新。
          </p>
          <label className="flex items-start gap-2 text-sm text-slate-600">
            <input type="checkbox" className="mt-1" checked={refreshExisting}
              onChange={(e) => setRefreshExisting(e.target.checked)} />
            <span>同时把已有的按文档刷新(发布新版本;会覆盖手工改过的描述,有未发布草稿的会跳过)</span>
          </label>
        </Modal>
      )}
      <ApiDocImportModal
        open={apiDocOpen}
        onClose={() => setApiDocOpen(false)}
        onCreated={(id) => { setApiDocOpen(false); openInModal(navigate, location, `/tools/tool/detail?id=${id}`); }}
      />

      <ConfirmDialog
        open={!!deleteTarget}
        title="删除 Tool"
        description={<>确定删除工具「<span className="font-bold text-slate-800">{deleteTarget?.name}</span>」？<br />删除后工具将停用并归档，不可恢复。</>}
        loading={deleting}
        forceEnabled
        onConfirm={confirmDelete}
        onClose={() => { if (!deleting) setDeleteTarget(null); }}
      />
    </div>
  );
};

export default ToolList;
