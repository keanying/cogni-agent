import React, { useState } from 'react';
import { HelpCircle, Plus, Trash2, Check, AlertCircle } from 'lucide-react';
import { cn } from '../../lib/utils';
import { MarkdownFieldsEditor } from './MarkdownFields';

/** 字段旁的「?」说明。说明文案来自后端模板,不在前端硬编码。 */
export const FieldHint: React.FC<{ text: string }> = ({ text }) => {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-flex">
      <button type="button" onClick={() => setOpen((v) => !v)} onBlur={() => setOpen(false)}
              aria-label="字段说明"
              className="text-slate-300 transition-colors hover:text-blue-500">
        <HelpCircle size={13} />
      </button>
      {open && (
        <span className="absolute left-5 top-0 z-30 w-80 rounded-xl border border-slate-200 bg-white p-3 text-[12px] font-normal leading-relaxed text-slate-600 shadow-xl">
          {text}
        </span>
      )}
    </span>
  );
};

export const FieldLabel: React.FC<{ label: string; hint?: string; required?: boolean }> = ({ label, hint, required }) => (
  <div className="mb-1.5 flex items-center gap-1.5">
    <span className="text-sm font-semibold text-slate-700">{label}</span>
    {required && <span className="text-rose-500">*</span>}
    {hint && <FieldHint text={hint} />}
  </div>
);

/** 键值对编辑器(请求头用)。比让人手写 JSON 少一整类语法错误。 */
export const KVEditor: React.FC<{
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  placeholderKey?: string; placeholderValue?: string;
}> = ({ value, onChange, placeholderKey = 'Header 名', placeholderValue = '值(支持 {{变量}})' }) => {
  const rows = Object.entries(value || {});
  const setRow = (i: number, k: string, v: string) => {
    const next: Record<string, string> = {};
    rows.forEach(([ok, ov], idx) => { if (idx === i) next[k] = v; else next[ok] = ov; });
    onChange(next);
  };
  return (
    <div className="space-y-2">
      {rows.map(([k, v], i) => (
        <div key={i} className="flex gap-2">
          <input value={k} onChange={(e) => setRow(i, e.target.value, v)} placeholder={placeholderKey}
                 className="w-2/5 rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-blue-400" />
          <input value={v} onChange={(e) => setRow(i, k, e.target.value)} placeholder={placeholderValue}
                 className="flex-1 rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-blue-400" />
          <button type="button" onClick={() => { const n = { ...value }; delete n[k]; onChange(n); }}
                  className="rounded-lg px-2 text-slate-300 transition-colors hover:text-rose-500">
            <Trash2 size={15} />
          </button>
        </div>
      ))}
      <button type="button" onClick={() => onChange({ ...value, '': '' })}
              className="flex items-center gap-1.5 rounded-lg border border-dashed border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-500 transition-colors hover:border-blue-400 hover:text-blue-600">
        <Plus size={13} />添加一行
      </button>
    </div>
  );
};

/** JSON Schema 编辑器:实时校验 + 一键格式化 + 填入示例。 */
export const SchemaEditor: React.FC<{
  value: any; onChange: (v: any) => void; example?: any; rows?: number;
}> = ({ value, onChange, example, rows = 10 }) => {
  const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);
  // Markdown 模式:直接贴接口文档里「请求参数」那一节。
  // 手写 JSON Schema 在嵌套参数上尤其难写,而写错了模型就按错的传参 ——
  // 接口回一个业务错误,看起来像「接口有问题」,其实是 schema 少了个字段。
  const [mdMode, setMdMode] = useState(false);

  const apply = (t: string) => {
    setText(t);
    try {
      const parsed = JSON.parse(t || '{}');
      setError(null);
      onChange(parsed);
    } catch (e: any) {
      // 只提示不阻断:输入过程中必然经过"暂时非法"的中间态,一边打字一边弹错很烦人
      setError(e.message);
    }
  };

  if (mdMode) {
    return (
      <MarkdownFieldsEditor
        kind="request"
        onCancel={() => setMdMode(false)}
        onApply={(fields) => {
          // 字段表 → JSON Schema。存的仍是 Schema(执行层、校验层认的就是它),
          // Markdown 只是**录入方式**,不是新的存储格式 —— 多一种存储就多一处要同步。
          const properties: Record<string, any> = {};
          const required: string[] = [];
          for (const f of fields) {
            properties[f.name] = {
              type: f.type || 'string',
              ...(f.desc ? { description: f.desc } : {}),
              ...(f.example ? { example: f.example } : {}),
            };
            if (f.required) required.push(f.name);
          }
          const schema = { type: 'object', properties, ...(required.length ? { required } : {}) };
          apply(JSON.stringify(schema, null, 2));
          setMdMode(false);
        }} />
    );
  }

  return (
    <div className="space-y-2">
      <textarea value={text} onChange={(e) => apply(e.target.value)} rows={rows} spellCheck={false}
        className={cn('w-full rounded-xl border bg-[#0d1117] p-3 font-mono text-[12.5px] leading-relaxed text-slate-200 outline-none',
                      error ? 'border-rose-400' : 'border-slate-700 focus:border-sky-500')} />
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-[11px]">
          {error
            ? <><AlertCircle size={12} className="text-rose-500" /><span className="text-rose-500">JSON 语法错误:{error}</span></>
            : <><Check size={12} className="text-emerald-500" /><span className="text-slate-400">JSON 合法</span></>}
        </div>
        <div className="flex gap-3">
          <button type="button" onClick={() => { try { apply(JSON.stringify(JSON.parse(text), null, 2)); } catch { /* 非法时不动 */ } }}
                  className="text-[11px] font-medium text-slate-500 hover:text-blue-600">格式化</button>
          <button type="button" onClick={() => setMdMode(true)}
                  className="text-[11px] font-medium text-blue-600 hover:underline">按接口文档填</button>
          {example && (
            <button type="button" onClick={() => apply(JSON.stringify(example, null, 2))}
                    className="text-[11px] font-medium text-slate-500 hover:text-blue-600">填入示例</button>
          )}
        </div>
      </div>
    </div>
  );
};

/** 折叠式教程。默认收起,不占地方;需要时展开看。 */
export const Tutorial: React.FC<{ items: { title: string; body: string }[] }> = ({ items }) => {
  const [open, setOpen] = useState(false);
  if (!items?.length) return null;
  return (
    <div className="rounded-xl border border-blue-100 bg-blue-50/40">
      <button type="button" onClick={() => setOpen((v) => !v)}
              className="flex w-full items-center justify-between px-4 py-3 text-left">
        <span className="text-sm font-bold text-blue-900">使用说明({items.length} 节)</span>
        <span className="text-xs text-blue-600">{open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-blue-100 px-4 py-3">
          {items.map((it, i) => (
            <div key={i}>
              <div className="text-[12.5px] font-bold text-blue-900">{it.title}</div>
              <p className="mt-0.5 text-[12px] leading-relaxed text-blue-800/80">{it.body}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

/**
 * 响应字段说明编辑器(29 号):path/类型/单位/说明 行式编辑 + 可选 sample。
 * 消费方是执行链——工具首次成功返回后把字段表附给大模型,防误读空值/零值。
 */
export const ResponseSchemaEditor: React.FC<{
  value: any; onChange: (v: any) => void;
}> = ({ value, onChange }) => {
  // 早先按接口文档导入的工具,字段名存在 name 而不是 path —— 读进来时认一下,
  // 否则编辑页字段名整列是空的,一保存还会把名字彻底丢掉。
  const fields: Array<{ path: string; type: string; unit: string; desc: string }> =
    (((value?.fields as any) || []) as any[]).map((f) => ({ ...f, path: f.path ?? f.name ?? '' }));
  const sample = value?.sample ?? null;
  // Markdown 模式:直接贴接口文档里「返回参数」那一节。
  // 一行一行点「加字段」在嵌套结构(data[].profile[].uri)上根本点不下去 ——
  // 于是这一栏干脆不填,而不填的代价很隐蔽:结果压缩层不知道数据体在哪一层、每列是什么,
  // 上千行的返回只能原样截断,模型看到半截 JSON 还一本正经地作答。
  const [mdMode, setMdMode] = useState(false);

  const setFields = (fs: typeof fields) => onChange({ ...(value || {}), fields: fs, sample });
  const upd = (i: number, k: 'path' | 'type' | 'unit' | 'desc', v: string) => {
    const fs = fields.map((f, j) => (j === i ? { ...f, [k]: v } : f));
    setFields(fs);
  };

  const input = (v: string, onCh: (s: string) => void, ph: string, mono = false) => (
    <input value={v} onChange={(e) => onCh(e.target.value)} placeholder={ph} spellCheck={false}
      className={cn('h-8 w-full rounded-lg border border-slate-200 px-2 text-xs outline-none focus:border-blue-400',
        mono && 'font-mono')} />
  );

  if (mdMode) {
    return (
      <MarkdownFieldsEditor
        kind="response"
        onCancel={() => setMdMode(false)}
        onApply={(parsed) => {
          onChange({
            ...(value || {}),
            // 存的结构不变(path/type/unit/desc),Markdown 只是**录入方式**。
            // unit 字段表里没有,留空让人自己补 —— 猜一个单位比空着更危险。
            fields: parsed.map((f) => ({ path: f.name, type: f.type, unit: '', desc: f.desc })),
            sample,
          });
          setMdMode(false);
        }} />
    );
  }

  return (
    <div className="space-y-2">
      {fields.length === 0 && (
        <p className="rounded-lg bg-slate-50 px-3 py-2 text-[11px] text-slate-400">
          未配置响应字段说明。配置后,大模型首次拿到该工具的返回时会附上字段语义
          (空值/零值含义、单位),避免把「无销售」误读成「数据不可用」。
        </p>
      )}
      {fields.map((f, i) => (
        <div key={i} className="rounded-lg border border-slate-200 bg-white p-2 space-y-1.5">
          <div className="flex items-center gap-1.5">
            {input(f.path || '', (s) => upd(i, 'path', s), '字段路径,如 data.totalAmount', true)}
            <div className="w-20 shrink-0">{input(f.type || '', (s) => upd(i, 'type', s), '类型')}</div>
            <div className="w-16 shrink-0">{input(f.unit || '', (s) => upd(i, 'unit', s), '单位')}</div>
            <button type="button" onClick={() => setFields(fields.filter((_, j) => j !== i))}
              className="shrink-0 rounded-lg p-1.5 text-slate-300 hover:bg-red-50 hover:text-red-500">
              <Trash2 size={13} />
            </button>
          </div>
          <textarea value={f.desc || ''} onChange={(e) => upd(i, 'desc', e.target.value)}
            placeholder="说明(执行链注入给 LLM 的关键——空值/零值含义、单位、业务背景,如「无销售时为 null,≠错误」)"
            rows={1}
            className="w-full resize-y rounded-lg border border-slate-200 bg-slate-50/50 px-2 py-1.5 text-xs leading-relaxed text-slate-700 outline-none focus:border-blue-400 focus:bg-white" />
        </div>
      ))}
      <div className="flex items-center gap-2">
        <button type="button"
          onClick={() => setFields([...fields, { path: '', type: '', unit: '', desc: '' }])}
          className="flex items-center gap-1 rounded-lg bg-slate-100 px-2.5 py-1 text-[11px] font-bold text-slate-600 hover:bg-slate-200">
          <Plus size={12} />加字段
        </button>
        <button type="button" onClick={() => setMdMode(true)}
          className="text-[11px] font-medium text-blue-600 hover:underline">按接口文档填</button>
        <label className="flex cursor-pointer items-center gap-1 text-[11px] text-slate-500">
          <input type="checkbox" checked={sample !== null}
            onChange={(e) => onChange({ ...(value || {}), fields, sample: e.target.checked ? {} : null })}
            className="h-3 w-3 accent-blue-600" />
          附响应样例(sample)
        </label>
      </div>
      {sample !== null && (
        <textarea
          value={typeof sample === 'string' ? sample : JSON.stringify(sample, null, 2)}
          onChange={(e) => {
            try { onChange({ ...(value || {}), fields, sample: JSON.parse(e.target.value || '{}') }); }
            catch { onChange({ ...(value || {}), fields, sample: e.target.value }); }
          }}
          rows={5} spellCheck={false}
          className="w-full rounded-xl border border-slate-700 bg-[#0d1117] p-3 font-mono text-[12px] leading-relaxed text-slate-200 outline-none focus:border-sky-500" />
      )}
    </div>
  );
};
