import { useState, useMemo, createContext, useContext, useRef, useEffect } from "react";
import {
  LayoutDashboard, MessageSquare, Wrench, Boxes, Plug, BookOpen, ListTodo,
  UserCheck, FlaskConical, Settings, ChevronRight, ChevronDown, Search, Plus, Play,
  Database, Globe2, Images, CircleDot, Clock, CheckCircle2, XCircle,
  AlertTriangle, Shield, Zap, Send, PanelRightOpen, PanelLeftClose, PanelLeftOpen, RefreshCw,
  UserCog, Smartphone, KeyRound, MapPin, Trash2, Pencil,
  Share2, Globe, Lock, Users2, ScrollText, Code2, Copy, Ban, Sparkles,
  Bell, ChevronsUpDown, LogOut, X, Check, Loader2
} from "lucide-react";

/* ════════════════════════ 设计令牌 ════════════════════════ */
export const C = {
  bg: "#080b16", panel: "#0e1424", card: "#121a30", card2: "#161f3a",
  border: "rgba(148,163,184,0.12)", borderStrong: "rgba(148,163,184,0.22)",
  text: "#e8ecf5", muted: "#7c8aa8", faint: "#4c5875",
  primary: "#6366f1", primaryHi: "#818cf8", accent: "#22d3ee",
  green: "#34d399", amber: "#fbbf24", orange: "#fb923c", red: "#f87171",
  violet: "#a78bfa", rose: "#fb7185", teal: "#2dd4bf",
};
export const mono = { fontFamily: "'JetBrains Mono',monospace" };
export const ui = { fontFamily: "'Inter',sans-serif" };
export const shadow = "0 1px 2px rgba(0,0,0,0.4), 0 8px 24px -8px rgba(0,0,0,0.5)";
export const shadowLg = "0 4px 12px rgba(0,0,0,0.3), 0 24px 48px -12px rgba(0,0,0,0.6)";

/* ════════════════════════ Toast 系统 ════════════════════════ */
export const ToastCtx = createContext<(msg: string, kind?: "ok" | "err" | "info") => void>(() => {});
export function useToast() { return useContext(ToastCtx); }
export function ToastHost({ items }: { items: { id: number; msg: string; kind: string }[] }) {
  const icon = { ok: <Check size={14} color={C.green} />, err: <X size={14} color={C.red} />, info: <Bell size={14} color={C.accent} /> } as any;
  const bd = { ok: C.green, err: C.red, info: C.accent } as any;
  return (
    <div className="fixed top-4 right-4 z-[999] space-y-2" style={{ width: 300 }}>
      {items.map(t => (
        <div key={t.id} className="rounded-lg border px-3.5 py-2.5 flex items-center gap-2.5 backdrop-blur"
          style={{ background: "rgba(18,26,48,0.95)", borderColor: bd[t.kind] + "44", boxShadow: shadowLg, animation: "toastIn .25s ease" }}>
          {icon[t.kind]}
          <span className="text-[12.5px]" style={{ ...ui, color: C.text }}>{t.msg}</span>
        </div>
      ))}
      <style>{`@keyframes toastIn{from{opacity:0;transform:translateX(16px)}to{opacity:1;transform:translateX(0)}}`}</style>
    </div>
  );
}

/* ════════════════════════ 基础组件 ════════════════════════ */
export function Chip({ label, color, ghost }: { label: string; color: string; ghost?: boolean }) {
  return <span className="px-1.5 py-0.5 rounded-md text-[10.5px] font-semibold inline-flex items-center gap-1"
    style={{ ...mono, color, background: ghost ? "transparent" : color + "16", border: `1px solid ${color}3a` }}>{label}</span>;
}
export function Card({ title, extra, children, style, pad = true }: any) {
  return (
    <div className="rounded-xl border transition-shadow duration-200" style={{ background: C.card, borderColor: C.border, boxShadow: shadow, ...style }}>
      {title && (
        <div className="px-4 py-3 border-b flex items-center justify-between" style={{ borderColor: C.border }}>
          <span className="text-[12px] font-semibold tracking-wide" style={{ ...ui, color: C.text, opacity: 0.85 }}>{title}</span>
          {extra}
        </div>
      )}
      <div className={pad ? "p-4" : ""}>{children}</div>
    </div>
  );
}
export function Th({ children, w }: any) { return <th style={{ width: w, ...mono }} className="text-left px-3 py-2.5 text-[10.5px] font-semibold tracking-wider uppercase"><span style={{ color: C.faint }}>{children}</span></th>; }
export function Td({ children }: any) { return <td className="px-3 py-2.5 text-[12.5px] border-t" style={{ ...mono, color: C.text, borderColor: C.border }}>{children}</td>; }
export function Row({ children, onClick, hi }: any) {
  return <tr onClick={onClick} className="transition-colors duration-150" style={{ cursor: onClick ? "pointer" : "default", background: hi ? C.primary + "0c" : "transparent" }}
    onMouseEnter={e => !hi && (e.currentTarget.style.background = "rgba(148,163,184,0.04)")}
    onMouseLeave={e => !hi && (e.currentTarget.style.background = "transparent")}>{children}</tr>;
}
export function Btn({ children, primary, danger, ghost, small, onClick, disabled, loading }: any) {
  const [down, setDown] = useState(false);
  const base = small ? "px-2.5 py-1.5 text-[12px] gap-1.5" : "px-3.5 py-2 text-[13px] gap-1.5";
  const bg = primary ? C.primary : danger ? C.red + "14" : ghost ? "transparent" : "transparent";
  const bd = primary ? C.primary : danger ? C.red + "50" : C.border;
  const fg = primary ? "#fff" : danger ? C.red : C.text;
  return (
    <button disabled={disabled || loading} onClick={onClick}
      onMouseDown={() => setDown(true)} onMouseUp={() => setDown(false)} onMouseLeave={() => setDown(false)}
      className={`rounded-lg border flex items-center justify-center font-semibold transition-all duration-150 ${base}`}
      style={{ ...ui, color: fg, background: bg, borderColor: bd, opacity: disabled ? 0.45 : 1, transform: down ? "scale(0.97)" : "scale(1)", boxShadow: primary && !disabled ? "0 2px 8px rgba(99,102,241,0.35)" : "none" }}>
      {loading ? <Loader2 size={13} className="animate-spin" /> : children}
    </button>
  );
}
export function IconBtn({ icon: I, color, onClick, title }: any) {
  const [h, setH] = useState(false);
  return (
    <button onClick={onClick} title={title} onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
      className="w-7 h-7 rounded-md flex items-center justify-center transition-all duration-150"
      style={{ background: h ? (color || C.muted) + "18" : "transparent" }}>
      <I size={14} color={color || C.muted} />
    </button>
  );
}
export function Input({ placeholder, value, onChange, icon: I, small, type, onBlur, onKeyDown, autoFocus, disabled }: any) {
  const [f, setF] = useState(false);
  return (
    <div className="flex items-center gap-2 rounded-lg border px-2.5 transition-all duration-150" style={{ borderColor: f ? C.primary : C.border, background: C.panel, boxShadow: f ? `0 0 0 3px ${C.primary}22` : "none", height: small ? 32 : 36, opacity: disabled ? 0.5 : 1 }}>
      {I && <I size={14} color={C.faint} />}
      <input value={value} onChange={onChange} type={type} autoFocus={autoFocus} disabled={disabled}
        onFocus={() => setF(true)} onBlur={(e) => { setF(false); onBlur?.(e); }}
        onKeyDown={onKeyDown} placeholder={placeholder}
        className="flex-1 bg-transparent outline-none text-[12.5px]" style={{ ...ui, color: C.text }} />
    </div>
  );
}
export function Modal({ title, icon: I, color = C.violet, onClose, children, footer, w = 560 }: any) {
  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center p-6" style={{ background: "rgba(4,6,14,0.72)", backdropFilter: "blur(3px)", animation: "fadeIn .15s ease" }} onClick={onClose}>
      <div className="rounded-xl border flex flex-col max-h-[85vh]" style={{ width: w, background: C.card2, borderColor: C.borderStrong, boxShadow: shadowLg, animation: "slideUp .2s cubic-bezier(.16,1,.3,1)" }} onClick={e => e.stopPropagation()}>
        <div className="px-5 py-4 border-b flex items-center justify-between flex-shrink-0" style={{ borderColor: C.border }}>
          <div className="flex items-center gap-2">
            {I && <I size={16} color={color} />}
            <span className="text-[15px] font-bold" style={{ ...ui, color: C.text }}>{title}</span>
          </div>
          <IconBtn icon={X} onClick={onClose} />
        </div>
        <div className="p-5 overflow-y-auto">{children}</div>
        {footer && <div className="px-5 py-3.5 border-t flex justify-end gap-2 flex-shrink-0" style={{ borderColor: C.border }}>{footer}</div>}
      </div>
      <style>{`@keyframes fadeIn{from{opacity:0}to{opacity:1}}@keyframes slideUp{from{opacity:0;transform:translateY(12px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}`}</style>
    </div>
  );
}
export function Field({ label, children }: any) {
  return <div><div className="text-[11.5px] mb-1.5 font-medium" style={{ ...ui, color: C.muted }}>{label}</div>{children}</div>;
}
export function PageHead({ pid, module, title, sub }: { pid: string; module: string; title: string; sub?: string }) {
  return (
    <div className="mb-5">
      <div className="flex items-center gap-2 mb-1.5">
        <Chip label={pid} color={C.faint} ghost />
        <ChevronRight size={11} color={C.faint} />
        <span className="text-[11px]" style={{ ...mono, color: C.faint }}>{module}</span>
      </div>
      <div className="flex items-baseline gap-2.5">
        <span className="text-[22px] font-bold tracking-tight" style={{ ...ui, color: C.text }}>{title}</span>
      </div>
      {sub && <div className="text-[12.5px] mt-1" style={{ ...mono, color: C.muted }}>{sub}</div>}
    </div>
  );
}
export function Empty({ text }: { text: string }) {
  return <div className="py-10 text-center text-[12.5px]" style={{ ...mono, color: C.faint }}>{text}</div>;
}
export function ConfirmBar({ text, onConfirm, onCancel }: any) {
  return (
    <div className="flex items-center gap-2 rounded-lg border px-3 py-2" style={{ borderColor: C.red + "44", background: C.red + "0c" }}>
      <AlertTriangle size={13} color={C.red} />
      <span className="text-[12px] flex-1" style={{ ...ui, color: C.text }}>{text}</span>
      <Btn small onClick={onCancel}>取消</Btn>
      <Btn small danger onClick={onConfirm}>确认删除</Btn>
    </div>
  );
}
