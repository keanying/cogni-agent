import React, { useEffect, useState } from 'react';
import { AlertTriangle, Loader2 } from 'lucide-react';
import { Button } from './button';
import { cn } from '../../lib/utils';

/**
 * 全局确认/输入弹窗。
 *
 * 为什么要有这么个东西:项目里到处是 `if (!confirm('确认删除?')) return;`。
 * 浏览器原生弹窗有三个问题 —— 长得跟系统毫无关系(而且各浏览器各一套)、
 * 文案只能一行纯文本(说不清「删了会怎样」)、还会阻塞整个页面。
 * 但改造成本必须足够低,否则不会有人去改。所以这里做成**Promise 版**,
 * 调用处仍然是一行:
 *
 *     if (!await confirmDialog({ title: '归档会话', message: '…' })) return;
 *
 * 宿主 <ConfirmHost /> 在 App 里挂一次即可。没挂载时回退到原生 confirm,
 * 保证任何时候都不会出现「点了没反应」。
 */

export interface ConfirmOptions {
  title: string;
  /** 说清后果:删了什么、能不能恢复、影响谁。别只写「确认删除?」 */
  message?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 破坏性操作:确认键变红 */
  danger?: boolean;
  /** 传了就是输入框弹窗,resolve 出用户填的字符串;取消 resolve null */
  input?: { label?: string; placeholder?: string; defaultValue?: string; required?: boolean };
}

type Pending = ConfirmOptions & { resolve: (v: any) => void };

let mount: ((p: Pending) => void) | null = null;

/** 确认弹窗。确认 true / 取消 false。 */
export function confirmDialog(opts: ConfirmOptions): Promise<boolean> {
  if (!mount) {
    // 宿主没挂(极端情况:弹窗在 App 外被调用)——退回原生,功能不能因此失灵
    const text = [opts.title, typeof opts.message === 'string' ? opts.message : ''].filter(Boolean).join('\n');
    return Promise.resolve(window.confirm(text));
  }
  return new Promise((resolve) => mount!({ ...opts, resolve }));
}

/** 输入弹窗。返回填写内容;取消返回 null。 */
export function promptDialog(opts: ConfirmOptions & { input: NonNullable<ConfirmOptions['input']> }):
  Promise<string | null> {
  if (!mount) {
    return Promise.resolve(window.prompt(opts.title, opts.input.defaultValue || ''));
  }
  return new Promise((resolve) => mount!({ ...opts, resolve }));
}

export const ConfirmHost: React.FC = () => {
  const [item, setItem] = useState<Pending | null>(null);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    mount = (p) => { setValue(p.input?.defaultValue || ''); setBusy(false); setItem(p); };
    return () => { mount = null; };
  }, []);

  if (!item) return null;

  const isInput = !!item.input;
  const close = (v: any) => { setBusy(true); item.resolve(v); setItem(null); };
  const cancel = () => close(isInput ? null : false);
  const ok = () => {
    if (isInput && item.input?.required && !value.trim()) return;
    close(isInput ? value : true);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
         onClick={cancel}>
      <div className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-2 flex items-start gap-2.5">
          {item.danger && (
            <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-rose-50 text-rose-500">
              <AlertTriangle size={15} />
            </span>
          )}
          <h3 className="text-base font-bold text-slate-800">{item.title}</h3>
        </div>
        {item.message && (
          <div className="mb-5 whitespace-pre-wrap text-sm leading-relaxed text-slate-500">{item.message}</div>
        )}
        {isInput && (
          <div className="mb-5">
            {item.input!.label && <label className="mb-1 block text-xs font-bold text-slate-500">{item.input!.label}</label>}
            <input autoFocus value={value} onChange={(e) => setValue(e.target.value)}
              placeholder={item.input!.placeholder}
              onKeyDown={(e) => { if (e.key === 'Enter') ok(); if (e.key === 'Escape') cancel(); }}
              className="h-9 w-full rounded-lg border border-slate-200 px-3 text-sm outline-none focus:border-blue-400" />
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={cancel}>{item.cancelLabel || '取消'}</Button>
          <Button onClick={ok} disabled={busy || (isInput && !!item.input?.required && !value.trim())}
            className={cn(item.danger ? 'bg-rose-600 hover:bg-rose-700' : 'bg-[#2563eb] hover:bg-[#1d4ed8]', 'text-white')}>
            {busy && <Loader2 size={14} className="mr-1 animate-spin" />}{item.confirmLabel || '确认'}
          </Button>
        </div>
      </div>
    </div>
  );
};

export default ConfirmHost;
