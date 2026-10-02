
import React, { useEffect, useState } from 'react';
import {
  CheckCircle2, XCircle, ShieldAlert, User, AlertTriangle, Code2, Loader2, RefreshCw, X, Clock,
} from 'lucide-react';
import { Card, CardContent } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Avatar, AvatarFallback } from '../../components/ui/avatar';
import { cn } from '../../lib/utils';
import { motion, AnimatePresence } from 'framer-motion';
import { toast } from 'sonner';
import * as api from '../../lib/api';

const TABS = [
  { key: 'pending', label: '待审批' },
  { key: 'approved', label: '已通过' },
  { key: 'rejected', label: '已驳回' },
] as const;

const RISK_CLS: Record<string, string> = {
  high: 'bg-rose-50 text-rose-600 border-rose-100',
  medium: 'bg-amber-50 text-amber-600 border-amber-100',
  low: 'bg-emerald-50 text-emerald-600 border-emerald-100',
};

const ApprovalCenter: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'pending' | 'approved' | 'rejected'>('pending');
  const [items, setItems] = useState<any[]>([]);
  const [pendingCount, setPendingCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<any>(null);
  const [detail, setDetail] = useState<any>(null);
  const [opinion, setOpinion] = useState('');
  const [reasonError, setReasonError] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [cur, pend] = await Promise.all([
        api.listApprovals(activeTab),
        activeTab === 'pending' ? Promise.resolve(null) : api.listApprovals('pending'),
      ]);
      setItems(cur.items);
      setPendingCount(pend ? pend.items.length : cur.items.length);
    } catch (e: any) {
      toast.error(e.message || '加载审批列表失败');
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, [activeTab]);

  const openDetail = async (item: any) => {
    setSelected(item); setOpinion(''); setReasonError(false); setDetail(null);
    try { setDetail(await api.getApproval(item.id)); } catch { /* 详情拉取失败时仍展示列表已有字段 */ }
  };

  const resolve = async (approved: boolean) => {
    if (!approved && !opinion.trim()) { setReasonError(true); return; }
    setReasonError(false);
    setSubmitting(true);
    try {
      await api.resolveApproval(selected.id, approved, opinion);
      toast.success(approved ? '审批已通过，任务将从断点恢复执行' : '审批已驳回，任务已取消');
      setSelected(null);
      load();
    } catch (e: any) {
      toast.error(e.message || '操作失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500 relative">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">审批中心</h1>
          <p className="text-slate-500 text-sm mt-1">处理高风险工具调用及资源发布请求，确保全系统运行安全可控。</p>
        </div>
        <Button variant="outline" className="h-10 rounded-xl border-slate-200 gap-2 font-medium" onClick={load}>
          <RefreshCw size={15} />刷新
        </Button>
      </div>

      <div className="flex items-center gap-2 p-1 bg-slate-100 rounded-xl w-fit border">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setActiveTab(t.key)}
            className={cn('px-6 py-2 rounded-lg text-xs font-black uppercase tracking-widest transition-all flex items-center gap-2',
              activeTab === t.key ? 'bg-white text-blue-600 shadow-sm' : 'text-slate-500 hover:bg-white/50')}>
            {t.label}
            {t.key === 'pending' && pendingCount > 0 && (
              <Badge className="bg-rose-500 text-white border-none h-5 min-w-5 flex items-center justify-center p-0 px-1 rounded-full text-[10px]">{pendingCount}</Badge>
            )}
          </button>
        ))}
      </div>

      <div className="enterprise-table-container overflow-x-auto">
        <table className="enterprise-table">
          <thead>
            <tr><th>审批类型</th><th>关联任务</th><th>发起者</th><th>风险等级</th><th>发起时间</th><th className="text-right">操作</th></tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6} className="py-16 text-center"><Loader2 className="animate-spin text-slate-300 mx-auto" size={24} /></td></tr>
            ) : items.length === 0 ? (
              <tr><td colSpan={6} className="py-16 text-center text-slate-300">
                {activeTab === 'pending' ? '暂无待处理审批' : activeTab === 'approved' ? '暂无已通过记录' : '暂无已驳回记录'}
              </td></tr>
            ) : items.map((item) => (
              <tr key={item.id} className="cursor-pointer" onClick={() => openDetail(item)}>
                <td>
                  <div className="flex items-center gap-3">
                    <Badge variant="outline" className="h-7 px-2 font-bold text-[10px] rounded-lg shrink-0 uppercase bg-amber-50 text-amber-600 border-amber-100">
                      {item.type}
                    </Badge>
                  </div>
                </td>
                <td><span className="font-mono text-[11px] text-slate-500">{(item.related_task_id || '-').slice(0, 12)}</span></td>
                <td>
                  <div className="flex items-center gap-2">
                    <Avatar className="h-6 w-6 border rounded-full"><AvatarFallback className="text-[9px] bg-slate-100 text-slate-500">{(item.applicant_user_id || '?')[0]}</AvatarFallback></Avatar>
                    <span className="text-xs font-mono text-slate-600">{(item.applicant_user_id || '-').slice(0, 10)}</span>
                  </div>
                </td>
                <td><Badge variant="outline" className={cn('rounded-md font-black text-[10px] uppercase', RISK_CLS[item.risk_level] || RISK_CLS.low)}>{item.risk_level}</Badge></td>
                <td><span className="text-xs text-slate-500 tabular-nums">{new Date(item.created_at).toLocaleString()}</span></td>
                <td className="text-right">
                  <Button variant="ghost" size="sm" className="h-8 rounded-lg text-xs font-bold text-blue-600" onClick={(e) => { e.stopPropagation(); openDetail(item); }}>
                    {item.status === 'pending' ? '去处理' : '查看'}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <AnimatePresence>
        {selected && (
          <>
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              className="fixed inset-0 bg-black/40 backdrop-blur-sm z-40" onClick={() => setSelected(null)} />
            <motion.div initial={{ x: '100%' }} animate={{ x: 0 }} exit={{ x: '100%' }} transition={{ type: 'spring', damping: 30, stiffness: 300 }}
              className="fixed top-0 right-0 h-full w-full max-w-lg bg-white shadow-2xl z-50 flex flex-col">
              <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 shrink-0">
                <div className="flex items-center gap-2">
                  <ShieldAlert size={18} className="text-amber-500" />
                  <h3 className="text-base font-bold text-slate-800">审批详情</h3>
                </div>
                <button className="h-8 w-8 rounded-full hover:bg-slate-100 flex items-center justify-center text-slate-400" onClick={() => setSelected(null)}><X size={16} /></button>
              </div>

              <div className="flex-1 overflow-y-auto custom-scrollbar p-6 space-y-5">
                <div className="grid grid-cols-2 gap-3">
                  <div className="p-3 rounded-xl bg-slate-50 border border-slate-100">
                    <div className="text-[10px] text-slate-400 uppercase font-bold mb-1">审批类型</div>
                    <div className="text-sm font-bold text-slate-700">{selected.type}</div>
                  </div>
                  <div className="p-3 rounded-xl bg-slate-50 border border-slate-100">
                    <div className="text-[10px] text-slate-400 uppercase font-bold mb-1">风险等级</div>
                    <Badge variant="outline" className={cn('text-[10px] uppercase font-black', RISK_CLS[selected.risk_level])}>{selected.risk_level}</Badge>
                  </div>
                  <div className="p-3 rounded-xl bg-slate-50 border border-slate-100">
                    <div className="text-[10px] text-slate-400 uppercase font-bold mb-1">发起人</div>
                    <div className="text-xs font-mono text-slate-700 break-all">{selected.applicant_user_id}</div>
                  </div>
                  <div className="p-3 rounded-xl bg-slate-50 border border-slate-100">
                    <div className="text-[10px] text-slate-400 uppercase font-bold mb-1">关联任务</div>
                    <div className="text-xs font-mono text-slate-700 break-all">{selected.related_task_id}</div>
                  </div>
                </div>

                <div className="p-4 rounded-xl bg-amber-50 border border-amber-100 flex items-start gap-3">
                  <AlertTriangle size={16} className="text-amber-500 shrink-0 mt-0.5" />
                  <p className="text-xs text-amber-800 leading-relaxed">
                    通过后，执行引擎会从 LangGraph 检查点精确恢复该任务并继续执行；驳回后任务将被标记为已取消。
                  </p>
                </div>

                {detail?.context && (
                  <div>
                    <div className="text-[10px] text-slate-400 uppercase font-bold mb-2 flex items-center gap-1.5"><Code2 size={12} />上下文快照（待执行的工具调用）</div>
                    <pre className="bg-slate-900 text-emerald-400 rounded-xl p-4 text-[11px] font-mono overflow-x-auto max-h-64">{JSON.stringify(detail.context, null, 2)}</pre>
                  </div>
                )}

                {selected.status !== 'pending' && (
                  <div className="p-4 rounded-xl bg-slate-50 border border-slate-100 space-y-2">
                    <div className="flex items-center gap-2">
                      {selected.status === 'approved' ? <CheckCircle2 size={14} className="text-emerald-500" /> : <XCircle size={14} className="text-rose-500" />}
                      <span className="text-xs font-bold text-slate-700">{selected.status === 'approved' ? '已通过' : '已驳回'}</span>
                    </div>
                    {(detail?.opinion || selected.opinion) && <p className="text-xs text-slate-500 pl-6">意见：{detail?.opinion || selected.opinion}</p>}
                    {(detail?.approver_user_id) && <p className="text-[10px] text-slate-400 pl-6 font-mono">处理人：{detail.approver_user_id}</p>}
                  </div>
                )}
              </div>

              {selected.status === 'pending' && (
                <div className="border-t border-slate-100 p-6 space-y-3 shrink-0 bg-slate-50/50">
                  <div>
                    <input value={opinion} onChange={(e) => { setOpinion(e.target.value); setReasonError(false); }}
                      placeholder="审批意见（驳回时必填）"
                      className={cn('w-full h-11 px-4 rounded-xl border text-sm outline-none transition-colors',
                        reasonError ? 'border-rose-300 bg-rose-50 focus:ring-2 focus:ring-rose-100' : 'border-slate-200 focus:ring-2 focus:ring-blue-100')} />
                    {reasonError && <p className="text-[11px] text-rose-500 mt-1.5 pl-1">驳回操作必须填写审批意见</p>}
                  </div>
                  <div className="flex gap-3">
                    <Button disabled={submitting} className="flex-1 h-11 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl font-bold gap-2 shadow-lg shadow-emerald-600/20" onClick={() => resolve(true)}>
                      {submitting ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}通过
                    </Button>
                    <Button disabled={submitting} variant="outline" className="flex-1 h-11 border-rose-200 text-rose-600 hover:bg-rose-50 rounded-xl font-bold gap-2" onClick={() => resolve(false)}>
                      <XCircle size={15} />驳回
                    </Button>
                  </div>
                </div>
              )}
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  );
};

export default ApprovalCenter;
