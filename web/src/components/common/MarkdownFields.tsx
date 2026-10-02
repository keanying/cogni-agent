import React, { useState } from 'react';
import { AlertTriangle, Loader2, Table2, Wand2 } from 'lucide-react';
import { toast } from 'sonner';
import * as api from '../../lib/api';
import { cn } from '../../lib/utils';

/**
 * 字段表的 **Markdown 编辑模式**。
 *
 * 为什么要有它:运维手上现成的东西是一份接口文档,最自然的操作是把那一节整段贴进来。
 * 一行一行点「加字段」在简单接口上还行,遇到嵌套结构(`data[].profile[].uri`)
 * 点到第十行就没人愿意继续了 —— 于是这一栏干脆不填,而**不填的代价很隐蔽**:
 * 参数定义缺了,模型靠猜传参;响应字段说明缺了,结果压缩层不知道数据体在哪、每列是什么,
 * 上千行的返回只能原样截断。两种都不报错。
 *
 * 解析走**后端那一套表格解析器**(与整份文档解析同源),不在前端再写一份 ——
 * 两份解析器迟早对不上,而对不上的表现是「预览看着对、存进去不对」,最难查。
 */

export interface ParsedField {
  name: string; type: string; required: boolean; example: string; desc: string;
}

export const MarkdownFieldsEditor: React.FC<{
  kind: 'request' | 'response';
  /** 解析成功后回调,交给调用方写进各自的结构 */
  onApply: (fields: ParsedField[]) => void;
  /** 回到表格模式 */
  onCancel: () => void;
}> = ({ kind, onApply, onCancel }) => {
  const [md, setMd] = useState('');
  const [fields, setFields] = useState<ParsedField[] | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const sample = kind === 'request'
    ? `## 请求参数\n\n| 序号 | 字段 | 类型 | 必传 | 示例 | 注释 |\n| --- | --- | --- | --- | --- | --- |\n| 1 | scenicId | string | 是 | PFTSCA01018088 | 景区ID |\n| 2 | rows | int | 否 | 30 | 返回行数上限 |\n`
    : `## 返回参数\n\n| 序号 | 字段 | 类型 | 示例 | 注释 |\n| --- | --- | --- | --- | --- |\n| 1 | status | bool | true | 请求状态 |\n| 2 | data | array | [...] | 数据体 |\n|  | data[].profile[].uri | string | https://... | BI地址 |\n| 3 | code | int | 0 | 状态码 |\n`;

  const doParse = async () => {
    if (!md.trim()) { toast.error('先把字段表贴进来'); return; }
    setBusy(true);
    try {
      const r = await api.parseFieldTable({ markdown: md, kind });
      setFields(r.fields || []);
      setWarnings(r.warnings || []);
      if (r.fields?.length) toast.success(`解析出 ${r.fields.length} 个字段,确认后点「应用」`);
    } catch (e: any) {
      setFields(null);
      toast.error(e?.message || '解析失败');
    } finally { setBusy(false); }
  };

  return (
    <div className="space-y-2">
      <div className="rounded-lg bg-slate-50 px-3 py-2 text-[11px] leading-relaxed text-slate-500">
        把接口文档里{kind === 'request' ? '「请求参数」' : '「返回参数」'}那一节整段贴进来即可 ——
        贴一整份文档也认(会自动挑出对应那一节)。
        列名中英文都认、顺序随意、多一列少一列都行;嵌套字段直接写成
        <code className="mx-1 rounded bg-white px-1">data[].profile[].uri</code>。
      </div>

      <textarea
        value={md}
        onChange={(e) => { setMd(e.target.value); setFields(null); }}
        rows={10} spellCheck={false}
        placeholder={sample}
        className="w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs leading-relaxed outline-none focus:border-blue-400" />

      <div className="flex items-center gap-2">
        <button type="button" onClick={doParse} disabled={busy}
          className="flex items-center gap-1 rounded-lg bg-blue-600 px-2.5 py-1 text-[11px] font-bold text-white hover:bg-blue-700 disabled:opacity-50">
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Wand2 size={12} />}解析预览
        </button>
        <button type="button" onClick={() => setMd(sample)}
          className="text-[11px] text-blue-600 hover:underline">填入示例</button>
        <button type="button" onClick={onCancel}
          className="ml-auto flex items-center gap-1 text-[11px] text-slate-400 hover:text-slate-600">
          <Table2 size={12} />回到表格编辑
        </button>
      </div>

      {warnings.map((w, i) => (
        <div key={i} className="flex gap-1.5 rounded-lg border border-amber-100 bg-amber-50 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" />{w}
        </div>
      ))}

      {fields && fields.length > 0 && (
        <div className="rounded-lg border border-slate-200">
          <div className="border-b border-slate-100 px-2.5 py-1.5 text-[11px] font-bold text-slate-600">
            解析结果（{fields.length} 个字段）—— 确认无误再应用
          </div>
          <div className="max-h-48 divide-y divide-slate-50 overflow-y-auto">
            {fields.map((f, i) => (
              <div key={i} className="flex items-center gap-2 px-2.5 py-1.5 text-[11px]">
                <span className="font-mono text-slate-700">{f.name}</span>
                <span className="rounded bg-slate-100 px-1 text-[10px] text-slate-500">{f.type}</span>
                {f.required && <span className="rounded bg-rose-50 px-1 text-[10px] text-rose-500">必传</span>}
                <span className="truncate text-slate-400">{f.desc}</span>
              </div>
            ))}
          </div>
          <div className={cn('flex justify-end gap-2 border-t border-slate-100 px-2.5 py-1.5')}>
            <button type="button"
              onClick={() => { onApply(fields); toast.success(`已写入 ${fields.length} 个字段`); }}
              className="rounded-lg bg-emerald-600 px-2.5 py-1 text-[11px] font-bold text-white hover:bg-emerald-700">
              应用（覆盖当前字段）
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default MarkdownFieldsEditor;
