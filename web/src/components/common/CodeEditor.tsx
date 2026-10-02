import React, { useRef, useState } from 'react';
import { Maximize2, Minimize2, WrapText, Check } from 'lucide-react';
import { cn } from '../../lib/utils';
import { confirmDialog } from '../ui/confirm';

/**
 * 轻量代码编辑器:行号 + 语法着色 + 全屏 + 格式化。
 *
 * 为什么不引 Monaco / CodeMirror:Monaco 打包后约 3MB,而这里的使用场景是"偶尔写一段
 * 几十行的脚本或 SQL",为此让每个用户多下 3MB 不划算。这个实现用 textarea 叠一层
 * 着色后的 <pre>,滚动同步,行为和普通输入框一致(复制粘贴、撤销、输入法都正常)。
 * 后续如果确实需要自动补全/跳转定义,再换 Monaco 也不迟。
 */

// ── 语法着色 ─────────────────────────────────────────────────────────────
//
// **两条铁律**,违反任何一条都会让光标和字符对不上:
//
// 1. **单遍分词,不用「占位符暂存」。**
//    早期实现先把字符串/注释替换成占位符,再跑关键字和数字正则,最后还原。问题是
//    数字正则会匹配到占位符里的数字,把占位符本身破坏掉 —— 还原后就出现
//    `400 font-semibold">import sys` 这种把自己的 HTML 片段吐进代码里的乱码。
//    单遍扫描每个字符只归类一次,从根上避免。
//
// 2. **着色只能改颜色,不能改任何影响布局的属性。**
//    着色层浮在 textarea 上面,两层必须逐字符对齐。而 `font-semibold`(粗体比常规体宽)、
//    `px-0.5`(水平内边距)会让着色层的文字比 textarea 里的宽 —— 每遇到一个关键字就
//    错开一点,**越往后累积越多**。表现就是「点击位置和光标落点不一样,行越长偏得越厉害」,
//    全屏时行更长所以更明显。
//    所以下面只用 text-* 颜色类,绝不出现 font-weight / padding / letter-spacing。

type Rule = { type: string; re: RegExp };

const PY_RULES: Rule[] = [
  { type: 'comment', re: /^#[^\n]*/ },
  { type: 'string', re: /^([`'"])(?:\\.|(?!\1)[^\\\n])*\1/ },
  { type: 'keyword', re: /^\b(?:import|from|def|class|return|if|elif|else|for|while|try|except|finally|with|as|in|not|and|or|is|None|True|False|lambda|yield|raise|pass|break|continue|global|nonlocal|async|await|assert|del)\b/ },
  { type: 'number', re: /^\b\d+(?:\.\d+)?\b/ },
];

const SQL_RULES: Rule[] = [
  { type: 'comment', re: /^--[^\n]*/ },
  { type: 'string', re: /^'(?:[^']|'')*'/ },
  { type: 'keyword', re: /^\b(?:SELECT|FROM|WHERE|AND|OR|NOT|IN|LIKE|GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|ON|AS|COUNT|SUM|AVG|MAX|MIN|DISTINCT|HAVING|CASE|WHEN|THEN|ELSE|END|BETWEEN|IS|NULL|ASC|DESC|UNION|ALL|WITH)\b/i },
  { type: 'number', re: /^\b\d+(?:\.\d+)?\b/ },
];

// {{变量}} 是本系统的模板语法,优先级最高 —— 在哪种语言里都要显眼。
// 只给颜色,不加背景内边距(见上面铁律 2)。
const VAR_RULE: Rule = { type: 'var', re: /^\{\{\s*[a-zA-Z0-9_]+\s*\}\}/ };

const CLASS_OF: Record<string, string> = {
  comment: 'text-slate-500',
  string: 'text-emerald-400',
  keyword: 'text-sky-400',
  number: 'text-amber-300',
  var: 'text-pink-400',
};

function escapeHtml(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function highlight(code: string, language: string): string {
  const rules = [VAR_RULE, ...(language === 'sql' ? SQL_RULES : PY_RULES)];
  let out = '';
  let plain = '';     // 累积普通字符批量转义,少很多次函数调用
  let i = 0;

  const flushPlain = () => { if (plain) { out += escapeHtml(plain); plain = ''; } };

  while (i < code.length) {
    const rest = code.slice(i);
    let matched = false;
    for (const rule of rules) {
      const m = rule.re.exec(rest);
      if (m && m[0]) {
        flushPlain();
        out += '<span class="' + CLASS_OF[rule.type] + '">' + escapeHtml(m[0]) + '</span>';
        i += m[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) { plain += code[i]; i += 1; }
  }
  flushPlain();
  return out;
}

// 着色层与输入层共用的盒模型。两者必须**逐像素一致**,否则光标会和字符错位。
const FONT_SIZE = '13px';
const LINE_HEIGHT = '20px';   // 用固定 px 而不是倍数:倍数在不同字号下会取整,两层可能算出不同值
const FONT_FAMILY = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace";
const SHARED_BOX: React.CSSProperties = {
  margin: 0,
  padding: '12px',
  border: 0,
  fontFamily: FONT_FAMILY,
  fontSize: FONT_SIZE,
  lineHeight: LINE_HEIGHT,
  letterSpacing: 'normal',
  tabSize: 4,
  fontVariantLigatures: 'none',   // 连字会让字符宽度不等于 1ch,直接导致错位
};

interface Props {
  value: string;
  onChange: (v: string) => void;
  language?: 'python' | 'sql' | 'json';
  minRows?: number;
  placeholder?: string;
  /** 可选骨架(按用途)。给了就在工具栏显示「插入骨架」下拉。
   *  从零写插件容易忘掉 stdin/stdout 约定和异常处理,给骨架比给一句文档有效得多。 */
  skeletons?: { id: string; label: string; desc: string; code: string }[];
  /** 选了骨架后回调,外层可以顺带把入参定义也填上 */
  onPickSkeleton?: (id: string) => void;
}

const CodeEditor: React.FC<Props> = ({ value, onChange, language = 'python', minRows = 14, placeholder, skeletons, onPickSkeleton }) => {
  const [fullscreen, setFullscreen] = useState(false);
  const [wrap, setWrap] = useState(false);
  const [formatted, setFormatted] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const preRef = useRef<HTMLPreElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  // 行号列宽度按最大行号位数算,避免从 99 跳到 100 时整体左移一格、光标跟着偏
  const gutterWidth = `${Math.max(2, String(Math.max(value.split('\n').length, 1)).length) * 9 + 20}px`;

  const lines = value.split('\n');

  /** Tab 插入缩进而不是切走焦点——在代码框里按 Tab 跳到下一个控件是很烦人的。 */
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Tab') return;
    e.preventDefault();
    const ta = e.currentTarget;
    const { selectionStart: a, selectionEnd: b } = ta;
    const indent = language === 'sql' ? '  ' : '    ';
    const next = value.slice(0, a) + indent + value.slice(b);
    onChange(next);
    requestAnimationFrame(() => ta.setSelectionRange(a + indent.length, a + indent.length));
  };

  /** 只做保守的整理:统一缩进宽度、去行尾空白、压缩连续空行。
   *  不做真正的 AST 级格式化——那需要在浏览器里跑一个 Python 解析器,不值当,
   *  而且"看起来格式化了其实语义变了"比不格式化更危险。 */
  const format = () => {
    if (language === 'json') {
      try { onChange(JSON.stringify(JSON.parse(value), null, 2)); } catch { /* 非法 JSON 不动它 */ }
    } else {
      onChange(value.split('\n').map((l) => l.replace(/\s+$/, '')).join('\n').replace(/\n{3,}/g, '\n\n'));
    }
    setFormatted(true);
    setTimeout(() => setFormatted(false), 1200);
  };

  /** 输入层滚动时,着色层和行号列都要跟上,否则滚动后三者对不齐。 */
  const syncScroll = () => {
    const ta = taRef.current;
    if (!ta) return;
    if (preRef.current) {
      preRef.current.scrollTop = ta.scrollTop;
      preRef.current.scrollLeft = ta.scrollLeft;
    }
    if (gutterRef.current) gutterRef.current.scrollTop = ta.scrollTop;   // 行号只跟纵向
  };

  const body = (
    <div className={cn('rounded-xl border border-slate-700 bg-[#0d1117] overflow-hidden flex flex-col',
                       fullscreen && 'h-full')}>
      <div className="flex items-center justify-between border-b border-slate-700 bg-slate-800/50 px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">
          {language} · {lines.length} 行
        </span>
        <div className="flex items-center gap-1">
          {skeletons && skeletons.length > 0 && (
            <select
              value=""
              onChange={async (e) => {
                const sk = skeletons.find((x) => x.id === e.target.value);
                if (!sk) return;
                // 已有内容时确认再覆盖 —— 手写了一半被一键清掉是很恼人的
                if (value.trim() && !await confirmDialog({
                  title: `用「${sk.label}」骨架替换`,
                  message: '编辑器里现有的代码会被整段覆盖,且无法撤销。',
                  confirmLabel: '替换',
                  danger: true,
                })) return;
                onChange(sk.code);
                onPickSkeleton?.(sk.id);
              }}
              title="插入骨架"
              className="mr-1 rounded border border-slate-600 bg-slate-800 px-1.5 py-1 font-mono text-[10px] text-slate-300 outline-none">
              <option value="">插入骨架…</option>
              {skeletons.map((sk) => <option key={sk.id} value={sk.id}>{sk.label}</option>)}
            </select>
          )}
          <button type="button" onClick={() => setWrap((v) => !v)} title={wrap ? '关闭自动换行' : '开启自动换行'}
                  className={cn('rounded p-1.5 transition-colors', wrap ? 'text-sky-400 bg-sky-400/10' : 'text-slate-400 hover:text-slate-200')}>
            <WrapText size={14} />
          </button>
          <button type="button" onClick={format} title="整理格式"
                  className="rounded p-1.5 text-slate-400 transition-colors hover:text-slate-200">
            {formatted ? <Check size={14} className="text-emerald-400" /> : <span className="font-mono text-[11px]">{'{ }'}</span>}
          </button>
          <button type="button" onClick={() => setFullscreen((v) => !v)} title={fullscreen ? '退出全屏' : '全屏编辑'}
                  className="rounded p-1.5 text-slate-400 transition-colors hover:text-slate-200">
            {fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
        </div>
      </div>

      {/*
        几何统一:**着色层与输入层都用 absolute inset-0**,高度完全由外层容器决定。

        之前的写法是 textarea 用 `rows={minRows}` + `h-full`,着色层用 `absolute inset-0`。
        rows 会**参与布局撑高 textarea**,于是两层算出来的高度不一致、各自独立滚动,
        光标位置就和看到的字符对不上;全屏时 rows 变成 undefined 又是另一套几何,
        所以全屏和非全屏的偏移量还不一样——这正是你观察到的现象。
        现在高度只由容器的 minHeight/h-full 控制,两层永远同尺寸。
      */}
      <div className="relative flex flex-1 overflow-hidden"
           style={fullscreen ? undefined : { minHeight: `calc(${minRows} * ${LINE_HEIGHT} + 24px)` }}>
        {/* 行号列。必须跟随内容一起纵向滚动,否则滚动后行号和代码对不上 */}
        <div ref={gutterRef}
             className="shrink-0 select-none overflow-hidden border-r border-slate-800 bg-slate-900/40 text-right text-slate-600"
             style={{ ...SHARED_BOX, paddingLeft: 12, paddingRight: 8, width: gutterWidth }}>
          {lines.map((_, i) => <div key={i}>{i + 1}</div>)}
        </div>

        <div className="relative flex-1 overflow-hidden">
          <pre ref={preRef} aria-hidden="true"
               className="pointer-events-none absolute inset-0 overflow-auto text-slate-200"
               style={{ ...SHARED_BOX, whiteSpace: wrap ? 'pre-wrap' : 'pre',
                        overflowWrap: wrap ? 'break-word' : 'normal' }}
               dangerouslySetInnerHTML={{ __html: highlight(value, language) + '\n' }} />
          <textarea
            ref={taRef} value={value} onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown} onScroll={syncScroll}
            spellCheck={false} placeholder={placeholder}
            className="absolute inset-0 resize-none bg-transparent text-transparent caret-sky-400 outline-none"
            style={{ ...SHARED_BOX, whiteSpace: wrap ? 'pre-wrap' : 'pre',
                     overflowWrap: wrap ? 'break-word' : 'normal', overflow: 'auto' }}
          />
        </div>
      </div>
    </div>
  );

  if (!fullscreen) return body;
  return (
    <div className="fixed inset-0 z-50 bg-slate-950/80 p-6 backdrop-blur-sm" onKeyDown={(e) => e.key === 'Escape' && setFullscreen(false)}>
      <div className="mx-auto h-full max-w-6xl">{body}</div>
    </div>
  );
};

export default CodeEditor;
