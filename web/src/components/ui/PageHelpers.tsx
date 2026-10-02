import React from 'react';
import { X, CheckCircle2, Copy, ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
import { Card } from './card';
import { Button } from './button';
import { cn } from '../../lib/utils';
import { toast } from 'sonner';

// ─── 状态中英文映射(卡片上所有状态值统一展示为「英文(中文)」) ──
export const STATUS_CN: Record<string, string> = {
  PUBLISHED: '已发布',
  DRAFT: '草稿',
  ACTIVE: '启用',
  INACTIVE: '停用',
  DISABLED: '停用',
  ONLINE: '在线',
  OFFLINE: '离线',
};

// ─── ID 展示 + 一键复制 ─────────────────────────────────────
export const CopyId: React.FC<{ id: string; className?: string; showFull?: boolean }> = ({ id, className, showFull = false }) => (
  <span className={cn('inline-flex items-center gap-1.5 text-[10px] text-slate-400 font-mono', className)} title={id}>
    <span className="truncate">{showFull ? id : id.length > 18 ? `${id.slice(0, 18)}…` : id}</span>
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        navigator.clipboard?.writeText(id).catch?.();
        toast.success('ID 已复制');
      }}
      className="shrink-0 text-slate-300 transition-colors hover:text-blue-600"
      title="复制 ID"
    >
      <Copy size={12} />
    </button>
  </span>
);

// ─── 通用弹窗 ──────────────────────────────────────────────
export const Modal: React.FC<{
  title: string; onClose: () => void; children: React.ReactNode;
  footer?: React.ReactNode; wide?: boolean;
}> = ({ title, onClose, children, footer, wide }) => (
  <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={onClose}>
    <Card className={cn("bg-white shadow-l3 rounded-2xl", wide ? "w-full max-w-2xl" : "w-full max-w-md")} onClick={(e) => e.stopPropagation()}>
      <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
        <h3 className="text-base font-bold text-slate-800">{title}</h3>
        <button className="h-8 w-8 rounded-full hover:bg-slate-100 flex items-center justify-center text-slate-400" onClick={onClose}><X size={16} /></button>
      </div>
      <div className="p-6 space-y-4 max-h-[65vh] overflow-y-auto custom-scrollbar">{children}</div>
      {footer && <div className="px-6 py-4 border-t border-slate-100 flex justify-end gap-2">{footer}</div>}
    </Card>
  </div>
);

// ─── 新密码展示弹窗 ────────────────────────────────────────
export const PasswordReveal: React.FC<{
  account: string; password: string; onClose: () => void;
}> = ({ account, password, onClose }) => (
  <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4" onClick={onClose}>
    <div className="w-full max-w-md rounded-2xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
      <div className="mb-1 flex items-center gap-2">
        <CheckCircle2 size={18} className="text-emerald-500" />
        <h3 className="text-base font-bold text-slate-900">密码重置成功</h3>
      </div>
      <p className="mb-4 text-xs text-slate-500">请把下面的密码转交给使用者。<b>本弹窗关闭后无法再次查看</b>（密码以哈希存储，系统也读不出明文）。</p>
      <div className="mb-4 space-y-2 rounded-xl bg-slate-50 p-4">
        <div className="flex justify-between text-sm"><span className="text-slate-500">账号</span><span className="font-mono font-semibold text-slate-800">{account}</span></div>
        <div className="flex items-center justify-between text-sm">
          <span className="text-slate-500">新密码</span>
          <span className="flex items-center gap-2">
            <code className="rounded bg-white px-2 py-1 font-mono text-sm font-bold text-blue-600 shadow-sm">{password}</code>
            <button onClick={() => { navigator.clipboard?.writeText(password); toast.success('已复制'); }} className="text-slate-400 transition-colors hover:text-blue-600" title="复制"><Copy size={14} /></button>
          </span>
        </div>
      </div>
      <p className="mb-4 text-[11px] leading-relaxed text-amber-700">使用者首次登录时会被要求修改密码。</p>
      <Button className="w-full bg-[#2563eb]" onClick={onClose}>我已记录</Button>
    </div>
  </div>
);

// ─── 确认弹窗 ──────────────────────────────────────────────
export const ConfirmModal: React.FC<{
  title: string; message: string; confirmLabel?: string;
  danger?: boolean; onConfirm: () => void; onCancel: () => void; loading?: boolean;
}> = ({ title, message, confirmLabel = '确认', danger, onConfirm, onCancel, loading }) => (
  <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4" onClick={onCancel}>
    <div className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
      <h3 className="text-base font-bold text-slate-800 mb-2">{title}</h3>
      <p className="text-sm text-slate-500 mb-6">{message}</p>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={onCancel} disabled={loading}>取消</Button>
        <Button className={danger ? 'bg-red-600 hover:bg-red-700' : 'bg-[#2563eb]'} onClick={onConfirm} disabled={loading}>
          {loading && <Loader2 size={14} className="animate-spin mr-1" />}{confirmLabel}
        </Button>
      </div>
    </div>
  </div>
);

// ─── 分页器 ────────────────────────────────────────────────
export const PAGE_SIZES = [10, 20, 50];

export const Paginator: React.FC<{
  page: number; pageSize: number; total: number; totalPages: number;
  onPageChange: (p: number) => void; onPageSizeChange: (s: number) => void;
}> = ({ page, pageSize, total, totalPages, onPageChange, onPageSizeChange }) => (
  <div className="flex items-center justify-between px-4 py-3 border-t border-slate-100 bg-slate-50/50">
    <div className="flex items-center gap-2 text-xs text-slate-500">
      <span>每页</span>
      <select value={pageSize} onChange={(e) => onPageSizeChange(Number(e.target.value))} className="h-7 px-1.5 rounded border border-slate-200 text-xs bg-white outline-none">
        {PAGE_SIZES.map((s) => <option key={s} value={s}>{s}</option>)}
      </select>
      <span>条 · 共 {total} 条</span>
    </div>
    <div className="flex items-center gap-1">
      <Button variant="outline" size="sm" className="h-7 w-7 p-0" disabled={page <= 1} onClick={() => onPageChange(page - 1)}><ChevronLeft size={14} /></Button>
      <span className="text-xs text-slate-500 px-2">{page} / {totalPages || 1}</span>
      <Button variant="outline" size="sm" className="h-7 w-7 p-0" disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}><ChevronRight size={14} /></Button>
    </div>
  </div>
);
