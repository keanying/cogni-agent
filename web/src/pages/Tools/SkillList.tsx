
import React, { useEffect, useState } from 'react';
import { Plus, Search, Settings2, Trash2, Zap, Loader2 } from 'lucide-react';
import { CopyId } from '../../components/ui/PageHelpers';
import { Card } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { useLocation, useNavigate } from 'react-router-dom';
import { openInModal } from '../../lib/modalNav';
import { CARD_GRID, CardMeta } from '../../components/common/CardMeta';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { MODE_LABELS } from '../../lib/modes';
import { ConfirmDialog } from '../../components/ui/ConfirmDialog';

// 展示名统一取 lib/modes(内部码仍是 L0/L1/L2,只有给人看的地方右移一位)
const MODE_META: Record<string, { label: string; color: string }> = {
  L0: { label: MODE_LABELS.L0.label, color: 'bg-slate-100 text-slate-600 border-slate-200' },
  L1: { label: MODE_LABELS.L1.label, color: 'bg-blue-50 text-blue-600 border-blue-100' },
  L2: { label: MODE_LABELS.L2.label, color: 'bg-purple-50 text-purple-600 border-purple-100' },
};

const SkillList: React.FC = () => {
  const [skills, setSkills] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<any>(null);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  const load = async () => {
    setLoading(true);
    try { setSkills((await api.listSkills()).items); }
    catch (e: any) { toast.error(e.message || '加载 Skill 列表失败'); }
    finally { setLoading(false); }
  };
  // 查看/修改在弹窗里做;弹窗关闭(跳回列表)时 location.key 变化 → 重新拉一次
  useEffect(() => { load(); }, [location.key]);

  // G3 收敛(13 号文档 Phase 4):一键发布 = 固化当前主表内容为首个已发布版本
  // (无 base 派生草稿 -> POST /versions/{id}/publish),不再直改主表 status;
  // 已发布版本的管理(新版本/下线)统一进编辑页版本视图。与后端 update_skill
  // status 载荷移除配套(见 routes.py)。
  const publishFirstVersion = async (skill: any, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const d: any = await api.createResourceVersion('skill', skill.id, { note: '列表一键发布:固化当前内容' });
      const rid = d?.id;
      if (!rid) throw new Error('草稿创建失败,请刷新后重试');
      const r: any = await api.publishResourceVersion(rid);
      if (r.ok === false) { toast.error(r.message || r.hint || '发布被阻断'); return; }
      toast.success(r.version != null ? `已发布 v${r.version}` : '发布成功');
      if (r.hint) toast.warning(r.hint);
      load();
    } catch (err: any) { toast.error(err.message || '发布失败'); }
  };

  const confirmDelete = async (force?: boolean) => {
    if (!deleteTarget) return;
    setDeleting(true);
    try { await api.deleteSkill(deleteTarget.id, !!force); toast.success(force ? '已强制删除并归档' : '已删除'); setDeleteTarget(null); load(); }
    catch (err: any) { toast.error(err.message || '删除失败'); }
    finally { setDeleting(false); }
  };

  const filtered = skills.filter((s) => !keyword || s.name.includes(keyword));

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">Skill 中心</h1>
          <p className="text-slate-500 text-sm mt-1">编排提示词与工具集，定义智能体的核心业务逻辑。</p>
        </div>
        <Button onClick={() => navigate('/tools/skill/new')} className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white rounded-xl gap-2 shadow-lg shadow-blue-200 h-11 px-6">
          <Plus size={18} />
          新建 Skill
        </Button>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/50 backdrop-blur-sm p-4">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
          <Input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="搜索技能名称..." className="pl-10 h-11 bg-white border-slate-200 rounded-xl focus-visible:ring-[#2563eb]" />
        </div>
      </Card>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="animate-spin text-slate-300" size={28} /></div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-20 text-slate-300 text-sm">暂无 Skill，点击右上角新建</div>
      ) : (
        <div className={CARD_GRID}>
          {filtered.map((skill) => {
            const mode = MODE_META[skill.suggested_mode] || { label: skill.suggested_mode, color: 'bg-slate-50 text-slate-500' };
            return (
              <Card key={skill.id} onClick={() => openInModal(navigate, location, `/tools/skill/detail?id=${skill.id}`)}
                className={cn('group border-slate-200 shadow-sm hover:shadow-md hover:border-blue-200 transition-all bg-white overflow-hidden flex flex-col cursor-pointer',
                  (skill.published_count ?? 0) === 0 && 'opacity-75')}>
                <div className="p-4 flex-1">
                  <div className="flex items-start gap-2.5">
                    <div className="h-8 w-8 shrink-0 rounded-lg bg-indigo-50 flex items-center justify-center text-indigo-600">
                      <Zap size={15} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <h3 className="text-sm font-bold text-slate-900 group-hover:text-blue-600 transition-colors truncate" title={skill.name}>{skill.name}</h3>
                      <CopyId id={skill.id} />
                    </div>
                    <Badge variant="outline" className={cn('shrink-0 rounded-md font-bold text-[10px] px-1.5 py-0', mode.color)}>{mode.label}</Badge>
                  </div>
                  <p className="mt-2 text-xs text-slate-500 line-clamp-2" title={skill.description}>
                    {skill.description || `绑定 ${skill.tool_ids?.length || 0} 个工具`}
                  </p>
                  <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                    {(skill.published_count ?? 0) > 0 ? (
                      // 徽标 = 已发布版本数(published_count,后端按 published 状态聚合;
                      // 15 号文档:无默认版本概念,不再信主表 status / default_version)
                      <Badge variant="outline" className="rounded-md border-emerald-100 bg-emerald-50 text-emerald-600 font-medium text-[10px] px-1.5 py-0">
                        已发布 {skill.published_count} 版
                      </Badge>
                    ) : (
                      <button onClick={(e) => publishFirstVersion(skill, e)}
                        title="把当前内容固化为首个已发布版本"
                        className="h-5 px-1.5 rounded-md text-[10px] font-bold bg-amber-50 text-amber-600 hover:bg-amber-100">
                        未发布 · 一键发布
                      </button>
                    )}
                    <Badge variant="outline" className="rounded-md border-slate-100 bg-slate-50 text-slate-500 font-medium text-[10px] px-1.5 py-0">
                      {skill.tool_ids?.length || 0} 个工具
                    </Badge>
                    {(skill.assigned_count ?? 0) > 0 && (
                      <Badge variant="outline" className="rounded-md border-blue-100 bg-blue-50 text-blue-600 font-medium text-[10px] px-1.5 py-0">
                        已指派 {skill.assigned_count} 人
                      </Badge>
                    )}
                  </div>
                </div>
                <div className="px-4 py-2 bg-slate-50/50 border-t flex items-center justify-between gap-2">
                  <CardMeta item={skill} />
                  <div className="flex shrink-0 gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg" title="修改" onClick={(e) => { e.stopPropagation(); openInModal(navigate, location, `/tools/skill/edit?id=${skill.id}`); }}>
                      <Settings2 size={14} className="text-slate-500" />
                    </Button>
                    <Button variant="ghost" size="icon" className="h-7 w-7 rounded-lg text-rose-500 hover:text-rose-600 hover:bg-rose-50" title="删除" onClick={(e) => { e.stopPropagation(); setDeleteTarget(skill); }}>
                      <Trash2 size={14} />
                    </Button>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        title="删除 Skill"
        description={<>确定删除技能「<span className="font-bold text-slate-800">{deleteTarget?.name}</span>」？<br />删除后将归档，历史版本与提示词记录保留，不可恢复。</>}
        loading={deleting}
        forceEnabled
        onConfirm={confirmDelete}
        onClose={() => { if (!deleting) setDeleteTarget(null); }}
      />
    </div>
  );
};

export default SkillList;
