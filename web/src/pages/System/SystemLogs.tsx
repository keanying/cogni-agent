
import React, { useEffect, useState } from 'react';
import {
  Search, ShieldCheck, Loader2, RefreshCw, X, User, Clock, Hash, AlertCircle, Info,
} from 'lucide-react';
import { Card, CardContent } from '../../components/ui/card';
import { Button } from '../../components/ui/button';
import { Badge } from '../../components/ui/badge';
import { Input } from '../../components/ui/input';
import { cn } from '../../lib/utils';
import { motion, AnimatePresence } from 'framer-motion';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { useAppStore } from '../../store/useAppStore';

/**
 * 说明:原型稿这一页是按"HTTP 全链路请求日志(trace_id / status code / duration)"设计的,
 * 但后端「系统日志」模块实际记录的是**敏感事件审计流水**(gov_audit:事件类型/操作人/
 * 景区/详情快照),两者不是一回事——没有请求级的 status code 和耗时字段。这里按后端
 * 真实的数据结构重做:以事件类型为主线,支持按类型过滤 + 查看详情快照,而不是硬套一个
 * 后端根本不产出的"接口调用瀑布图"。
 */

const EVENT_META: Record<string, { label: string; cls: string }> = {
  UnauthorizedAccess: { label: '越权访问', cls: 'bg-rose-50 text-rose-600 border-rose-100' },
  HighRiskToolCall: { label: '高风险工具调用', cls: 'bg-amber-50 text-amber-600 border-amber-100' },
  ApprovalResolved: { label: '审批处理', cls: 'bg-blue-50 text-blue-600 border-blue-100' },
  ToolDeleted: { label: '工具删除', cls: 'bg-rose-50 text-rose-600 border-rose-100' },
  AgentPublished: { label: 'Agent 发布', cls: 'bg-emerald-50 text-emerald-600 border-emerald-100' },
  LoginFailed: { label: '登录失败', cls: 'bg-amber-50 text-amber-600 border-amber-100' },
  GuardrailTripped: { label: '护栏熔断', cls: 'bg-rose-50 text-rose-600 border-rose-100' },
};

const SystemLogs: React.FC = () => {
  const { me } = useAppStore();
  const [logs, setLogs] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [eventFilter, setEventFilter] = useState('');
  const [selected, setSelected] = useState<any>(null);

  const allowed = me?.role === 'super_admin' || me?.role === 'group_admin';

  const load = async () => {
    setLoading(true);
    try { setLogs((await api.listAuditLogs(eventFilter || undefined)).items); }
    catch (e: any) { toast.error(e.message || '加载审计日志失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => { if (allowed) load(); else setLoading(false); }, [eventFilter, allowed]);

  if (!allowed) {
    return (
      <div className="flex flex-col items-center justify-center py-24 gap-3">
        <ShieldCheck size={40} className="text-slate-200" />
        <p className="text-sm text-slate-400">系统日志仅 super_admin / group_admin 可查看</p>
      </div>
    );
  }

  const eventTypes = Array.from(new Set(logs.map((l) => l.event_type)));
  const filtered = logs.filter((l) => !keyword
    || (l.event_type || '').toLowerCase().includes(keyword.toLowerCase())
    || (l.actor_user_id || '').includes(keyword)
    || (l.scenic_id || '').includes(keyword));

  return (
    <div className="space-y-6 animate-in fade-in slide-in-from-bottom-4 duration-500 relative">
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">系统日志</h1>
          <p className="text-slate-500 text-sm mt-1">敏感事件审计流水，记录越权访问、高风险调用、审批处理等关键动作。</p>
        </div>
        <Button variant="outline" className="h-10 rounded-xl border-slate-200 gap-2 font-medium" onClick={load}><RefreshCw size={15} />刷新</Button>
      </div>

      <Card className="border-slate-200 shadow-sm overflow-hidden bg-white/50 backdrop-blur-sm">
        <CardContent className="p-4">
          <div className="flex flex-col md:flex-row gap-3">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
              <Input value={keyword} onChange={(e) => setKeyword(e.target.value)} placeholder="按事件类型 / 操作人 / 景区搜索..." className="pl-10 h-11 bg-white border-slate-200 rounded-xl focus-visible:ring-blue-500" />
            </div>
            <select value={eventFilter} onChange={(e) => setEventFilter(e.target.value)}
              className="h-11 px-4 rounded-xl border border-slate-200 bg-white text-sm outline-none font-medium text-slate-600 min-w-[160px]">
              <option value="">全部事件类型</option>
              {eventTypes.map((t) => <option key={t} value={t}>{EVENT_META[t]?.label || t}</option>)}
            </select>
          </div>
        </CardContent>
      </Card>

      <div className="enterprise-table-container overflow-x-auto">
        <table className="enterprise-table">
          <thead><tr><th>事件类型</th><th>操作人</th><th>景区</th><th>发生时间</th><th className="text-right">操作</th></tr></thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={5} className="py-16 text-center"><Loader2 className="animate-spin text-slate-300 mx-auto" size={24} /></td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={5} className="py-16 text-center text-slate-300">暂无审计记录</td></tr>
            ) : filtered.map((log) => {
              const meta = EVENT_META[log.event_type] || { label: log.event_type, cls: 'bg-slate-50 text-slate-600 border-slate-200' };
              return (
                <tr key={log.id} className="cursor-pointer" onClick={() => setSelected(log)}>
                  <td><Badge variant="outline" className={cn('rounded-md font-bold text-[10px]', meta.cls)}>{meta.label}</Badge></td>
                  <td><div className="flex items-center gap-1.5"><User size={12} className="text-slate-400" /><span className="text-xs font-mono text-slate-600">{log.actor_user_id || '系统'}</span></div></td>
                  <td><span className="text-xs text-slate-500">{log.scenic_id || '集团'}</span></td>
                  <td><span className="text-xs text-slate-500 tabular-nums">{new Date(log.created_at).toLocaleString()}</span></td>
                  <td className="text-right"><Button variant="ghost" size="sm" className="h-8 text-xs font-bold text-blue-600">查看详情</Button></td>
                </tr>
              );
            })}
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
                <h3 className="text-base font-bold text-slate-800">审计事件详情</h3>
                <button className="h-8 w-8 rounded-full hover:bg-slate-100 flex items-center justify-center text-slate-400" onClick={() => setSelected(null)}><X size={16} /></button>
              </div>
              <div className="flex-1 overflow-y-auto custom-scrollbar p-6 space-y-4">
                <div className="grid grid-cols-2 gap-3">
                  <div className="p-3 rounded-xl bg-slate-50 border border-slate-100"><div className="text-[10px] text-slate-400 uppercase font-bold mb-1">事件类型</div>
                    <Badge variant="outline" className={cn('text-[10px]', (EVENT_META[selected.event_type] || {}).cls)}>{EVENT_META[selected.event_type]?.label || selected.event_type}</Badge></div>
                  <div className="p-3 rounded-xl bg-slate-50 border border-slate-100"><div className="text-[10px] text-slate-400 uppercase font-bold mb-1">发生时间</div>
                    <div className="text-xs text-slate-700">{new Date(selected.created_at).toLocaleString()}</div></div>
                  <div className="p-3 rounded-xl bg-slate-50 border border-slate-100"><div className="text-[10px] text-slate-400 uppercase font-bold mb-1">操作人</div>
                    <div className="text-xs font-mono text-slate-700 break-all">{selected.actor_user_id || '系统'}</div></div>
                  <div className="p-3 rounded-xl bg-slate-50 border border-slate-100"><div className="text-[10px] text-slate-400 uppercase font-bold mb-1">景区</div>
                    <div className="text-xs text-slate-700">{selected.scenic_id || '集团级'}</div></div>
                </div>
                {selected.detail && (
                  <div>
                    <div className="text-[10px] text-slate-400 uppercase font-bold mb-2 flex items-center gap-1.5"><Hash size={12} />详情快照</div>
                    <pre className="bg-slate-900 text-emerald-400 rounded-xl p-4 text-[11px] font-mono overflow-x-auto max-h-96">{JSON.stringify(selected.detail, null, 2)}</pre>
                  </div>
                )}
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  );
};

export default SystemLogs;
