import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Layers, RefreshCw, Check, ChevronDown, FilePlus2, History,
  Rocket, Trash2, Undo2, Users, Loader2,
} from 'lucide-react';
import { Button } from '../ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '../ui/card';
import { Badge } from '../ui/badge';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { confirmDialog } from '../ui/confirm';

type VersionStatus = api.VersionStatus;

const STATUS_STYLE: Record<VersionStatus, string> = {
  published: 'bg-blue-50 text-blue-600 border-blue-200',
  draft: 'bg-amber-50 text-amber-600 border-amber-200',
  offline: 'bg-slate-100 text-slate-400 border-slate-200',
};

const STATUS_LABEL: Record<VersionStatus, string> = {
  published: '已发布',
  draft: '编辑中',
  offline: '已下线',
};

interface Props {
  resourceType: 'agent' | 'skill' | 'tool';
  resourceId: string;
  /** 外部触发刷新（如详情页弹窗保存草稿后自增） */
  refreshKey?: number;
  /** 草稿被创建/放弃时回调（详情页据此刷新 frozen 状态、恢复编辑按钮） */
  onDraftChanged?: () => void;
  /** 父级已加载的版本数据,首次挂载直接复用,避免重复请求 */
  initialVersions?: api.ResourceVersion[];
  initialAvailable?: boolean;
}

const VersionHistoryPanel: React.FC<Props> = ({ resourceType, resourceId, refreshKey = 0, onDraftChanged, initialVersions, initialAvailable }) => {
  const [versions, setVersions] = useState<api.ResourceVersion[]>(initialVersions ?? []);
  const [available, setAvailable] = useState(initialAvailable ?? true);
  const [loading, setLoading] = useState(initialVersions === undefined);
  const [busy, setBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  /** 已消费的 refreshKey:外部递增时即使有初始数据也全量刷新 */
  const lastRefreshKey = useRef(refreshKey);

  const load = useCallback(async () => {
    if (!resourceId) return;
    setLoading(true);
    try {
      const r = await api.listResourceVersions(resourceType, resourceId);
      setVersions(r.items);
      setAvailable(r.available);
    } catch (e: any) {
      toast.error(e.message || '版本列表加载失败');
    } finally {
      setLoading(false);
    }
  }, [resourceType, resourceId]);

  useEffect(() => {
    // 外部递增 refreshKey → 全量刷新(父级可能未同步新数据)
    if (refreshKey !== lastRefreshKey.current) {
      lastRefreshKey.current = refreshKey;
      load();
      return;
    }
    // 父级已提供数据 → 直接复用同步,不发请求(含 StrictMode 二次调用,均不请求)
    if (initialVersions !== undefined) {
      setVersions(initialVersions);
      if (initialAvailable !== undefined) setAvailable(initialAvailable);
      setLoading(false);
      return;
    }
    // 无初始数据 → 挂载时自行加载
    load();
  }, [load, refreshKey, initialVersions, initialAvailable]);

  const run = async (id: string, action: () => Promise<any>, ok: string) => {
    setBusy(id);
    try {
      const r = await action();
      toast.success(r.message || ok);
      await load();
      onDraftChanged?.();
    } catch (e: any) {
      toast.error(e.message || '操作失败');
    } finally {
      setBusy(null);
    }
  };

  const confirmThen = async (msg: string, id: string, action: () => Promise<any>, ok: string) => {
    // msg 沿用调用处原本的整段文案:第一行当标题,其余当正文说明后果
    const [title, ...rest] = msg.split('\n');
    if (!await confirmDialog({
      title: title.replace(/[?？]$/, ''),
      message: rest.join('\n').trim() || undefined,
      danger: /下线|放弃|删除|丢弃/.test(title),
    })) return;
    run(id, action, ok);
  };

  if (loading) {
    return (
      <Card className="border-slate-200 shadow-sm">
        <CardContent className="p-8 flex justify-center"><Loader2 className="animate-spin text-slate-300" size={22} /></CardContent>
      </Card>
    );
  }

  // 草稿(version=null)排最后;正式版本号倒序
  const sorted = [...versions].sort((a, b) => (b.version ?? -1) - (a.version ?? -1));

  return (
    <Card className="border-slate-200 shadow-sm overflow-hidden">
      <CardHeader className="border-b bg-slate-50/50 py-4 px-6 flex flex-row items-center justify-between">
        <CardTitle className="text-sm font-black flex items-center gap-2 uppercase tracking-widest text-slate-600">
          <Layers size={16} className="text-indigo-600" /> 版本历史
          {!available && <Badge className="bg-rose-50 text-rose-600 border-none text-[10px] font-black ml-1">无可用版本</Badge>}
        </CardTitle>
        <Button variant="ghost" size="icon" className="h-8 w-8 rounded-lg text-slate-400 hover:text-blue-600" onClick={load} title="刷新">
          <RefreshCw size={15} className={loading ? 'animate-spin' : ''} />
        </Button>
      </CardHeader>
      <CardContent className="p-4 bg-slate-50/30">
        {sorted.length === 0 ? (
          <div className="text-center py-8 text-xs text-slate-400 font-medium space-y-2">
            <History className="mx-auto text-slate-300" size={28} />
            <div>暂无版本记录：当前展示的是主表内容（未进入版本管理）。</div>
            <div className="text-[10px] text-slate-300">15 号起无「默认版本」：发布后需到分发中心按用户指派才生效。</div>
          </div>
        ) : (
          <div className="space-y-3">
            {sorted.map((v) => {
              const isDraft = v.status === 'draft';
              const isOffline = v.status === 'offline';
              const time = v.published_at || v.created_at;
              return (
                <div key={v.id} className="rounded-2xl border border-slate-200 bg-white shadow-sm transition-all">
                  <div className="p-4">
                    <div className="flex items-center justify-between flex-wrap gap-2">
                      <div className="flex items-center gap-2.5">
                        <span className="text-sm font-black text-slate-900">v{v.version}</span>
                        <Badge className={`border text-[10px] font-black ${STATUS_STYLE[v.status]}`}>{STATUS_LABEL[v.status]}</Badge>
                        {resourceType === 'agent' && !!v.assigned_count && (
                          <Badge className="bg-indigo-50 text-indigo-600 border-none text-[10px] font-black gap-1">
                            <Users size={10} /> 已指派 {v.assigned_count} 位用户
                          </Badge>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5">
                        {isDraft && (
                          <>
                            <Button size="sm" className="h-7 rounded-lg text-[11px] font-bold gap-1 bg-amber-500 hover:bg-amber-600 text-white"
                              disabled={busy === v.id} onClick={() => confirmThen(
                                `发布草稿 v${v.version}？发布不会自动对任何人生效(15 号无默认),需到分发中心按用户指派。`,
                                v.id, () => api.publishResourceVersion(v.id), '已发布')}>
                              <Rocket size={12} /> 发布
                            </Button>
                            <Button size="sm" variant="outline" className="h-7 rounded-lg text-[11px] font-bold text-slate-500 gap-1"
                              disabled={busy === v.id} onClick={() => confirmThen(
                                `放弃草稿 v${v.version}？未发布的编辑将丢失。`, v.id,
                                () => api.discardResourceVersion(v.id), '草稿已放弃')}>
                              <Trash2 size={12} /> 放弃
                            </Button>
                          </>
                        )}
                        {!isDraft && (
                          <Button size="sm" variant="outline" className="h-7 rounded-lg text-[11px] font-bold text-slate-500 gap-1"
                            disabled={busy === v.id}
                            onClick={() => confirmThen(
                              `下线 v${v.version}？被用户指派或上层资源引用的版本会被后端拒绝下线。`, v.id,
                              () => api.offlineResourceVersion(v.id), '已下线')}>
                            <Undo2 size={12} /> 下线
                          </Button>
                        )}
                        {!isDraft && (
                          <Button size="sm" variant="outline" className="h-7 rounded-lg text-[11px] font-bold text-indigo-500 border-indigo-100 gap-1"
                            disabled={busy === v.id}
                            onClick={async () => {
                              const draft = versions.find((d) => d.status === 'draft');
                              if (draft && !await confirmDialog({
                                title: `基于 v${v.version} 新建编辑`,
                                message: `已有编辑中的草稿 v${draft.version},新建会先放弃它,草稿里未发布的改动将丢失。`,
                                confirmLabel: '放弃草稿并新建',
                                danger: true,
                              })) return;
                              const discard = draft ? api.discardResourceVersion(draft.id) : Promise.resolve();
                              discard.then(() => api.createResourceVersion(resourceType, resourceId, { base_version_id: v.id }))
                                .then((r) => { toast.success(`已基于 v${v.version} 创建草稿 v${r.version},可进入编辑区修改后发布。`); load(); onDraftChanged?.(); })
                                .catch((e: any) => toast.error(e.message || '新建编辑失败'));
                            }}>
                            <FilePlus2 size={12} /> 基于 v{v.version} 编辑
                          </Button>
                        )}
                        {isOffline && (
                          <Button size="sm" className="h-7 rounded-lg text-[11px] font-bold gap-1 bg-slate-800 hover:bg-slate-900 text-white"
                            disabled={busy === v.id} onClick={() => confirmThen(
                              `重新发布 v${v.version}？被用户指派或引用的版本会被后端拒绝下线。`,
                              v.id, () => api.rollbackResourceVersion(v.id), '已回滚')}>
                            <Undo2 size={12} /> 重新发布
                          </Button>
                        )}
                        <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg text-slate-300 hover:text-slate-500"
                          onClick={() => setExpanded(expanded === v.id ? null : v.id)}>
                          <ChevronDown size={14} className={`transition-transform ${expanded === v.id ? 'rotate-180' : ''}`} />
                        </Button>
                      </div>
                    </div>
                    <div className="flex items-center gap-3 mt-2 text-[11px] text-slate-400 font-medium">
                      <span>{time ? new Date(time).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}</span>
                      <span>·</span>
                      <span>操作人 {v.created_by || '—'}</span>
                      {isDraft && <span className="text-amber-500">· 保存只更新草稿，不影响任何客户</span>}
                    </div>
                    {v.note && <div className="mt-2 text-[11px] text-slate-500 bg-slate-50 rounded-lg px-2.5 py-1.5 border border-slate-100">{v.note}</div>}
                    {expanded === v.id && (
                      <pre className="mt-2 text-[10px] text-slate-500 bg-slate-50 rounded-xl p-3 border border-slate-100 overflow-auto max-h-56">
                        {JSON.stringify(v.snapshot || {}, null, 2)}
                      </pre>
                    )}
                  </div>
                </div>
              );
            })}
            <div className="flex items-center justify-between pt-1 px-1">
              <span className="text-[10px] text-slate-400 font-medium">
                15 号起无「默认版本」：发布不会自动对任何人生效，需到分发中心按用户指派；
                解除指派后该用户调用将报 agent_not_distributed（fail-closed）。
              </span>
              <Button size="sm" variant="outline" className="h-7 rounded-lg text-[11px] font-bold text-indigo-600 gap-1" disabled={!!busy}
                onClick={async () => {
                  try {
                    const r = await api.createResourceVersion(resourceType, resourceId, {});
                    toast.success(`已创建草稿 v${r.version}（基于当前内容），可进入编辑区修改后发布。`);
                    await load();
                    onDraftChanged?.();
                  } catch (e: any) {
                    toast.error(e.message || '新建版本失败');
                  }
                }}>
                <FilePlus2 size={12} /> 基于当前内容新建编辑
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
};

export default VersionHistoryPanel;
