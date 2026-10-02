import React, { useEffect, useRef, useState } from 'react';
import { Braces, Copy } from 'lucide-react';
import { toast } from 'sonner';
import * as api from '../../lib/api';

/**
 * 「插入系统参数」下拉:列出系统设置 · 参数配置里定义的参数,点一下得到 {{param_key}}。
 *
 * - 传 onPick:把引用交给调用方插入(Skill 提示词);
 * - 不传:复制到剪贴板(Tool / MCP 的表单字段多,没法知道光标在哪个框里)。
 * 不显示取值 —— 编辑工具的人不一定该看到商户账号。
 */
const MODE: Record<api.ParamMultiMode, string> = {
  single: '单值', fanout: '多值 · 每个值调用一次', list: '多值 · 列表一次传入', join: '多值 · 逗号拼接',
};

const EXAMPLE_SCHEMA = `{
  "type": "object",
  "properties": {
    "account": {
      "type": "string",
      "default": "{{ota_account}}"
    },
    "startTime": { "type": "string" }
  }
}`;

const ParamRefPicker: React.FC<{ onPick?: (ref: string) => void; className?: string }> = ({ onPick, className }) => {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<api.ParamRef[] | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || items) return;
    api.listParamRefs().then((r) => setItems(r.items)).catch((e) => { toast.error(e.message || '加载系统参数失败'); setItems([]); });
  }, [open, items]);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);

  const pick = async (p: api.ParamRef) => {
    setOpen(false);
    if (onPick) { onPick(p.ref); return; }
    try { await navigator.clipboard.writeText(p.ref); toast.success(`已复制 ${p.ref},粘贴到需要的字段里`); }
    catch { toast.message(`引用写法:${p.ref}`); }
  };

  return (
    <div ref={box} className={`relative inline-block ${className || ''}`}>
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs font-bold text-slate-600 hover:bg-slate-100">
        <Braces size={14} />{onPick ? '插入系统参数' : '引用系统参数'}
      </button>
      {open && (
        <div className={`absolute ${onPick ? 'left-0' : 'right-0'} top-9 z-50 w-96 rounded-xl border border-slate-200 bg-white p-2 shadow-xl`}>
          {items === null ? <div className="p-3 text-[12px] text-slate-400">加载中…</div>
            : items.length === 0 ? (
              <div className="p-3 text-[12px] leading-relaxed text-slate-400">
                还没有定义系统参数。到「系统设置 · 参数配置」里添加(如 OTA 商户账号)。
              </div>
            ) : items.map((p) => (
              <button key={p.param_key} type="button" onClick={() => pick(p)}
                className="block w-full rounded-lg px-3 py-2 text-left hover:bg-blue-50">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[12px] font-bold text-slate-700">{p.label}</span>
                  <span className="flex items-center gap-1 font-mono text-[11px] text-blue-600">{p.ref}{!onPick && <Copy size={11} />}</span>
                </div>
                <div className="mt-0.5 text-[10px] text-slate-400">
                  {MODE[p.multi_mode]}{p.is_secret ? ' · 敏感' : ''}{p.arg_names.length ? ` · 自动填入 ${p.arg_names.join('/')}` : ''}
                </div>
              </button>
            ))}
          <div className="mt-1 border-t border-slate-100 px-3 pt-2 text-[10px] leading-relaxed text-slate-400">
            执行时按当前景区取值(景区没分配用系统默认)。
          </div>
          {!onPick && (
            <div className="mt-2 space-y-1.5 px-3 pb-1 text-[10px] text-slate-500">
              <div className="font-bold text-slate-600">写法示例(以 {'{{ota_account}}'} 为例)</div>
              <div>① 请求体参数:在「参数定义」里给这个参数写 default,系统填、模型看不到</div>
              <pre className="overflow-x-auto rounded-lg bg-slate-50 p-2 font-mono text-[10px] leading-snug text-slate-700">{EXAMPLE_SCHEMA}</pre>
              <div>② 请求地址:<code className="font-mono text-slate-700">{'https://api.x.com/ota/{{ota_account}}/summary'}</code></div>
              <div>③ 请求头:<code className="font-mono text-slate-700">{'X-Account: {{ota_account}}'}</code></div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default ParamRefPicker;
