import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Copy, Check } from 'lucide-react';

/**
 * 消息内容渲染。
 *
 * 模型返回的是 Markdown,直接当纯文本显示的话,表格是一堆竖线、代码块是一堆反引号、
 * 列表是一堆星号 —— 用户得自己在脑子里解析。
 *
 * 代码块高亮**复用 CodeEditor 里那套单遍分词器**,不引 highlight.js/prism
 * (它们各自 100~300KB,而这里只需要给几种常见语言上个色)。
 */

const KEYWORDS: Record<string, RegExp> = {
  python: /^\b(?:import|from|def|class|return|if|elif|else|for|while|try|except|finally|with|as|in|not|and|or|is|None|True|False|lambda|yield|raise|pass|break|continue|async|await)\b/,
  sql: /^\b(?:SELECT|FROM|WHERE|AND|OR|NOT|IN|LIKE|GROUP\s+BY|ORDER\s+BY|LIMIT|JOIN|LEFT|RIGHT|INNER|ON|AS|COUNT|SUM|AVG|MAX|MIN|DISTINCT|HAVING|CASE|WHEN|THEN|ELSE|END|NULL|ASC|DESC)\b/i,
  json: /^\b(?:true|false|null)\b/,
  bash: /^\b(?:sudo|cd|ls|cat|echo|export|curl|grep|mkdir|rm|cp|mv|chmod|source)\b/,
};

function escapeHtml(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** 单遍分词着色。与 CodeEditor 同样的思路:每个字符只归类一次,不用占位符暂存
 *  ——占位符会被后续的数字/关键字正则误伤,导致把 HTML 片段吐进代码里。 */
/** 单遍分词着色。
 *  **只改颜色,不改任何影响宽度的属性**(font-weight / padding / letter-spacing)——
 *  这里虽然不是可编辑的叠加层,但与 CodeEditor 共用同一套约定,避免两处规则不一致;
 *  而且加粗会让等宽代码块的对齐(比如手工画的表格、缩进树)变形。 */
function highlightCode(code: string, lang: string): string {
  const kw = KEYWORDS[lang] || null;
  const commentRe = lang === 'sql' ? /^--[^\n]*/ : lang === 'python' || lang === 'bash' ? /^#[^\n]*/ : /^\/\/[^\n]*/;
  const strRe = /^("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`)/;
  const numRe = /^\b\d+(?:\.\d+)?\b/;

  let out = '', plain = '', i = 0;
  const flush = () => { if (plain) { out += escapeHtml(plain); plain = ''; } };

  while (i < code.length) {
    const rest = code.slice(i);
    let m: RegExpExecArray | null = null;
    let cls = '';
    if ((m = commentRe.exec(rest))) cls = 'text-slate-500';
    else if ((m = strRe.exec(rest))) cls = 'text-emerald-400';
    else if (kw && (m = kw.exec(rest))) cls = 'text-purple-400';
    else if ((m = numRe.exec(rest))) cls = 'text-amber-300';

    if (m && m[0]) {
      flush();
      out += `<span class="${cls}">${escapeHtml(m[0])}</span>`;
      i += m[0].length;
    } else {
      plain += code[i]; i += 1;
    }
  }
  flush();
  return out;
}

const CopyButton: React.FC<{ text: string; className?: string; label?: string }> = ({ text, className, label }) => {
  const [done, setDone] = useState(false);
  return (
    <button type="button" title="复制"
      onClick={() => {
        navigator.clipboard?.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1500);
      }}
      className={className ?? 'flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-slate-400 transition-colors hover:text-slate-700'}>
      {done ? <Check size={12} className="text-emerald-500" /> : <Copy size={12} />}
      {label && <span>{done ? '已复制' : label}</span>}
    </button>
  );
};

export { CopyButton };

/**
 * 注意:**不要依赖 `prose` 系列类**。它来自 @tailwindcss/typography 插件,本项目没装,
 * 写了也是空类。而 Tailwind 的 preflight 会把 h1~h6 的字号、ul/ol 的列表符号全部重置掉
 * ——结果是 Markdown "渲染了但看起来跟纯文本一样",很容易误判成没生效。
 * 所以下面每个元素的样式都显式写全,不借助任何插件。
 */
const Markdown: React.FC<{ content: string }> = ({ content }) => (
  <div className="max-w-none text-[14px] leading-relaxed text-slate-700">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code({ inline, className, children, ...props }: any) {
          const raw = String(children).replace(/\n$/, '');
          const lang = /language-(\w+)/.exec(className || '')?.[1] || '';
          // react-markdown v10 已移除 inline prop(恒 undefined,行内代码全部误判成
          // 块级、渲染成带"TEXT"头的深色代码框)。判定改按内容特征:
          // 无语言标注且无换行 = 行内代码(`` `xx` `` 反引号短串)。
          const isInline = !inline && !lang && !raw.includes('\n');
          if (isInline) {
            return <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[12.5px] text-rose-600" {...props}>{raw}</code>;
          }
          return (
            <div className="my-2 overflow-hidden rounded-xl border border-slate-700 bg-[#0d1117]">
              <div className="flex items-center justify-between border-b border-slate-700 bg-slate-800/60 px-3 py-1.5">
                <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">{lang || 'text'}</span>
                <CopyButton text={raw} className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-slate-400 hover:text-slate-100" />
              </div>
              <pre className="overflow-x-auto p-3 font-mono text-[12.5px] leading-relaxed text-slate-200">
                <code dangerouslySetInnerHTML={{ __html: highlightCode(raw, lang) }} />
              </pre>
            </div>
          );
        },
        table: ({ children }) => (
          <div className="my-2 overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">{children}</table>
          </div>
        ),
        th: ({ children }) => <th className="border border-slate-200 bg-slate-50 px-2 py-1.5 text-left font-semibold">{children}</th>,
        td: ({ children }) => <td className="border border-slate-200 px-2 py-1.5 align-top">{children}</td>,
        ul: ({ children }) => <ul className="my-1.5 list-disc space-y-0.5 pl-5">{children}</ul>,
        ol: ({ children }) => <ol className="my-1.5 list-decimal space-y-0.5 pl-5">{children}</ol>,
        h1: ({ children }) => <h1 className="mb-1.5 mt-3 text-[16px] font-bold text-slate-900">{children}</h1>,
        h2: ({ children }) => <h2 className="mb-1.5 mt-3 text-[15px] font-bold text-slate-900">{children}</h2>,
        h3: ({ children }) => <h3 className="mb-1 mt-2 text-[14px] font-bold text-slate-800">{children}</h3>,
        p: ({ children }) => <p className="my-1.5 whitespace-pre-wrap">{children}</p>,
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noreferrer" className="text-blue-600 underline hover:text-blue-700">{children}</a>
        ),
        blockquote: ({ children }) => (
          <blockquote className="my-2 border-l-4 border-slate-200 pl-3 text-slate-500">{children}</blockquote>
        ),
        // 以下几个 Tailwind preflight 都会重置,必须显式给样式,否则加粗不粗、分割线不见
        strong: ({ children }) => <strong className="font-bold text-slate-900">{children}</strong>,
        em: ({ children }) => <em className="italic">{children}</em>,
        li: ({ children }) => <li className="my-0.5">{children}</li>,
        hr: () => <hr className="my-3 border-t border-slate-200" />,
        h4: ({ children }) => <h4 className="mb-1 mt-2 text-[13.5px] font-bold text-slate-800">{children}</h4>,
        del: ({ children }) => <del className="text-slate-400 line-through">{children}</del>,
        input: ({ checked, type }: any) =>
          type === 'checkbox'
            ? <input type="checkbox" checked={!!checked} readOnly className="mr-1.5 align-middle" />
            : null,
      }}
    >
      {content}
    </ReactMarkdown>
  </div>
);

export default Markdown;
