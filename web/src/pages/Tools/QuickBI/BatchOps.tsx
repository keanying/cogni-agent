import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, CheckCircle2, ChevronDown, Copy, ListChecks, Loader2, Play, Sparkles, Timer, Trash2 } from 'lucide-react';
import { Button } from '../../../components/ui/button';
import { Modal } from '../../../components/ui/PageHelpers';
import * as api from '../../../lib/api';
import { cn } from '../../../lib/utils';
import { ConnectionSelect, inputCls, labelCls } from './shared';

/** 批量操作的类型:run 走批量执行接口,其余走批量运维接口(stream,逐条回报) */
export type BatchOpKey = 'run' | api.QbiBatchOp;

// 名称统一取 OP_LABEL(弹窗标题同一套叫法),这里只管图标、提示与顺序
const OP_ITEMS: { key: BatchOpKey; icon: React.ReactNode; hint: string; danger?: boolean }[] = [
  { key: 'run', icon: <Play size={13} />,
    hint: '串行执行所选问题(报告类只创建任务、不等待)' },
  { key: 'set_interval', icon: <Timer size={13} />,
    hint: '改加速的刷新 / 存储时间,并可一并开启 / 关闭加速' },
  { key: 'copy', icon: <Copy size={13} />,
    hint: '复制到指定连接:只换连接与归属景区(景区取连接自带的),其余照抄' },
  { key: 'insight', icon: <Sparkles size={13} />,
    hint: '能力切成「解读」,数据集按问题的主题(没挂标签时看数据域)自动匹配,命中几份挂几份' },
  // 删除压在最后一位:唯一不可逆的动作,离常用的几项越远越不容易被顺手点到
  { key: 'delete', icon: <Trash2 size={13} />, danger: true,
    hint: '删除所选问题(不可恢复);没有权限的行会按条失败' },
];

/** 弹窗标题用(与下拉同一套叫法,别处不用再抄一份) */
export const OP_LABEL: Record<BatchOpKey, string> = {
  run: '批量执行', set_interval: '批量修改加速时间', copy: '批量复制问题',
  insight: '批量解读数据集', delete: '批量删除',
};

// 每个操作「到底做了什么」:批量一次动几十条,日志里每条只有一句成败,
// 顶上不写清楚这一步的口径,看日志的人只能靠猜。
export const OP_ABOUT: Record<BatchOpKey, string[]> = {
  run: [
    '按勾选顺序串行执行,一条跑完再跑下一条(Quick BI 同一账号的报告任务不允许并发)。',
    '报告类只创建任务、不等它生成完,结果由后台收回,可在「运行记录」里看。',
    '单次最多 50 条。',
  ],
  set_interval: [
    '只改加速的刷新时间 / 存储时间;能力、目标数据一概不动。',
    '存储时间不填 = 保留原值;原值比新的刷新时间短时自动抬到刷新时间(存储必须 ≥ 刷新)。',
    '选了「开启 / 关闭加速」才动开关;未开加速的行在「不动开关」时会被跳过。',
  ],
  copy: [
    '每条按当前配置新建一条副本,原问题不动。',
    '只换「连接」与「归属景区」(景区取目标连接自带的);问题、口径、能力、目标数据、加速配置全部照抄。',
    '目标数据里引用的资产(看板 / 已登记数据集)属于原连接,换了连接需要重新选择 —— 日志里会提示。',
  ],
  insight: [
    '把所选问题的能力切成「解读」,并按各自的主题(三级标签第一级;没挂标签时看数据域)挂上匹配到的全部数据集。',
    '主题 = 概览 / 数据概览 / 总览 / 全域 时不按名称匹配,直接挂该连接下的全部数据集。',
    '其余主题按数据集名称里的主题词匹配,命中几份挂几份;挂多份时一次多表取数后再解读。',
    '问题与口径不动,改的只是「这条由谁、按哪几份数据来答」。匹配不到会按条失败(可先到数据资产登记)。',
  ],
  delete: [
    '逐条删除所选问题,不可恢复。',
    '没有权限的行(景区操作员 / 集团级问题)会按条失败,其余照常删除 —— 具体是哪几条看下面的红字。',
  ],
};

// ── 通用下拉(挂到 body) ────────────────────────────────────────────
// 整块内容都在 overflow-hidden 的卡片里,行内 absolute 会被裁掉(搜索后只剩一行时
// 必然越过卡片底边,表现为「点了却什么都没出来」)。挂到 body 上顺带拿到
// 「下方放不下自动翻上方、贴边内收」。
export const DropMenu: React.FC<{
  open: boolean;
  onOpenChange: (v: boolean) => void;
  trigger: React.ReactNode;
  width?: number;
  children: React.ReactNode;
}> = ({ open, onOpenChange, trigger, width = 176, children }) => {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  // 布局必须在 paint 前算,否则菜单会先在错误位置闪一下
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const a = anchorRef.current?.getBoundingClientRect();
      if (!a) return;
      const h = menuRef.current?.offsetHeight || 0;
      const below = a.bottom + 4;
      const top = below + h > window.innerHeight - 8 ? Math.max(8, a.top - h - 4) : below;
      setPos({ top, left: Math.max(8, Math.min(a.right - width, window.innerWidth - width - 8)) });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open, width]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const el = e.target as Node;
      if (anchorRef.current?.contains(el) || menuRef.current?.contains(el)) return;
      onOpenChange(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onOpenChange(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, onOpenChange]);

  return (
    <div className="relative" onClick={(e) => e.stopPropagation()}>
      <span ref={anchorRef} className="inline-flex" onClick={() => onOpenChange(!open)}>{trigger}</span>
      {open && createPortal(
        <div ref={menuRef} style={{ top: pos?.top ?? 0, left: pos?.left ?? 0, width, opacity: pos ? 1 : 0 }}
          className="fixed z-50 overflow-hidden rounded-lg border border-slate-200 bg-white py-1 shadow-lg">
          {children}
        </div>,
        document.body,
      )}
    </div>
  );
};

/** 「批量操作(N)」:几个动作收进一个下拉 —— 一行摆五个按钮会挤掉筛选条 */
export const BatchOpMenu: React.FC<{
  count: number; disabled?: boolean; busy?: boolean; onPick: (op: BatchOpKey) => void;
}> = ({ count, disabled, busy, onPick }) => {
  const [open, setOpen] = useState(false);
  return (
    <DropMenu open={open} onOpenChange={setOpen} width={190}
      trigger={
        <Button variant="outline" className="gap-1.5" disabled={disabled || !count}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <ListChecks size={14} />}
          批量操作({count})<ChevronDown size={12} />
        </Button>
      }>
      {OP_ITEMS.map((it) => (
        <button key={it.key} type="button" title={it.hint} disabled={!count}
          className={cn('flex w-full items-start gap-2 px-3 py-1.5 text-left text-[12px] disabled:opacity-50',
            it.danger ? 'text-rose-600 hover:bg-rose-50' : 'text-slate-700 hover:bg-slate-50')}
          onClick={() => { setOpen(false); onPick(it.key); }}>
          <span className={cn('mt-0.5', it.danger ? 'text-rose-400' : 'text-slate-400')}>{it.icon}</span>
          <span>{OP_LABEL[it.key]}</span>
        </button>
      ))}
    </DropMenu>
  );
};

// ── 批量修改加速时间 ────────────────────────────────────────────────
// 开关三态而不是一个勾:绝大多数时候是「只改时间、别动开关」—— 把「不动」也做成
// 一个显式选项,才不会手滑顺手开了加速。
const SWITCH_OPTIONS: { key: string; label: string; hint: string }[] = [
  { key: '', label: '不动开关', hint: '只改时间;未开加速的行会被跳过' },
  { key: '1', label: '开启加速', hint: '开启并套用这组时间' },
  { key: '0', label: '关闭加速', hint: '关闭(不需要填时间)' },
];
const MAX_MINUTES = 60 * 24 * 7;     // 与后端 _validate_fixed 同口径:最多 7 天

const minutesBrief = (m: number) => (m >= 60 && m % 60 === 0 ? `${m / 60} 小时` : `${m} 分钟`);

export const BatchIntervalModal: React.FC<{
  onCancel: () => void; onConfirm: (interval: number, ttl: number | null, enabled: boolean | null) => void;
}> = ({ onCancel, onConfirm }) => {
  const [iv, setIv] = useState('60');
  const [ttl, setTtl] = useState('');
  // '' = 不动开关(只改时间) / '1' = 一并开启 / '0' = 关闭加速
  const [sw, setSw] = useState('');
  const ivN = Math.trunc(Number(iv) || 0);
  const ttlN = ttl.trim() ? Math.trunc(Number(ttl) || 0) : null;
  const turningOff = sw === '0';
  const ivOk = ivN >= 1 && ivN <= MAX_MINUTES;
  const ttlOk = ttlN === null || (ttlN >= ivN && ttlN <= MAX_MINUTES);
  const valid = turningOff || (ivOk && ttlOk);
  return (
    <Modal title={OP_LABEL.set_interval} onClose={onCancel}
      footer={<>
        <Button variant="outline" onClick={onCancel}>取消</Button>
        <Button className={turningOff ? 'bg-rose-600 text-white' : 'bg-[#2563eb]'} disabled={!valid}
          onClick={() => onConfirm(ivN, ttlN, sw === '' ? null : sw === '1')}>应用</Button>
      </>}>
      <div className="space-y-3">
        <div>
          <div className={labelCls}>是否开启加速</div>
          <div className="flex gap-1.5">
            {SWITCH_OPTIONS.map((o) => (
              <button key={o.key} type="button" title={o.hint} onClick={() => setSw(o.key)}
                className={cn('rounded-md border px-2.5 py-1 text-[11px]',
                  sw === o.key ? (o.key === '0' ? 'border-rose-300 bg-rose-50 text-rose-700'
                    : 'border-blue-300 bg-blue-50 text-blue-700')
                    : 'border-slate-200 text-slate-600 hover:bg-slate-50')}>
                {o.label}
              </button>
            ))}
          </div>
        </div>
        <div className={cn('grid grid-cols-2 gap-3', turningOff && 'opacity-40')}>
          <div>
            <div className={labelCls}>刷新时间(分钟)</div>
            <input className={inputCls} value={iv} inputMode="numeric" disabled={turningOff}
              onChange={(e) => setIv(e.target.value)} />
          </div>
          <div>
            <div className={labelCls}>存储时间(分钟,选填)</div>
            <input className={inputCls} value={ttl} inputMode="numeric" disabled={turningOff}
              placeholder="不填 = 原值(不足刷新时间时自动抬上来)" onChange={(e) => setTtl(e.target.value)} />
          </div>
        </div>
        <div className={cn('flex flex-wrap gap-1.5', turningOff && 'opacity-40')}>
          {[30, 60, 180, 360, 720, 1440].map((m) => (
            <button key={m} type="button" disabled={turningOff} onClick={() => setIv(String(m))}
              className={cn('rounded-md border px-2 py-1 text-[11px]',
                ivN === m ? 'border-blue-300 bg-blue-50 text-blue-700' : 'border-slate-200 text-slate-600 hover:bg-slate-50')}>
              每 {minutesBrief(m)}
            </button>
          ))}
        </div>
        {!turningOff && !valid && (
          <div className="text-[11px] text-rose-500">
            {!ivOk ? `刷新时间需在 1 ~ ${MAX_MINUTES} 分钟之间` : '存储时间不能小于刷新时间,且不超过 7 天'}
          </div>
        )}
        <div className="rounded-lg bg-slate-50 p-2.5 text-[11px] leading-relaxed text-slate-500">
          {turningOff
            ? <>关闭所选问题的加速。没有权限的行会按条失败。</>
            : sw === '1'
              ? <>开启所选问题的加速,并套用上面这组时间。其他字段不动。</>
              : <>只改所选问题的加速时间,开关、能力、目标数据一概不动;未开加速的行会被跳过。</>}
        </div>
      </div>
    </Modal>
  );
};

// ── 批量复制问题 ────────────────────────────────────────────────────
// 交互上只给连接:景区从连接自带的景区取 —— 让人再填一遍景区就会填出
// 「连接是 A 景区、归属填了 B 景区」这种错位。
export const BatchCopyModal: React.FC<{
  connections: api.QbiConnection[]; onCancel: () => void; onConfirm: (connId: string) => void;
}> = ({ connections, onCancel, onConfirm }) => {
  const [connId, setConnId] = useState((connections.find((c) => c.is_default) || connections[0])?.id || '');
  const conn = connections.find((c) => c.id === connId);
  return (
    <Modal title={OP_LABEL.copy} onClose={onCancel}
      footer={<>
        <Button variant="outline" onClick={onCancel}>取消</Button>
        <Button className="bg-[#2563eb]" disabled={!connId} onClick={() => onConfirm(connId)}>下一步</Button>
      </>}>
      <div className="space-y-3">
        <div>
          <div className={labelCls}>复制到哪个连接</div>
          <ConnectionSelect items={connections} value={connId} onChange={setConnId} className="w-full" />
        </div>
        <div className="rounded-lg bg-slate-50 p-2.5 text-[11px] leading-relaxed text-slate-500">
          变化的只有两项:<span className="text-slate-700">连接</span> 与
          <span className="text-slate-700">归属景区</span>(取这条连接自带的:{conn?.scenic_id || '集团级'})。<br />
          问题、口径、能力、目标数据、加速配置全部照抄 —— 复制的目的是把同一套问法
          铺到另一个景区的账号下,改了口径就没法横向对比。
        </div>
      </div>
    </Modal>
  );
};

// ── 批量日志弹窗 ────────────────────────────────────────────────────
// 批量改几十条时,「成功 N / 失败 M」回答不了「漏的是哪一条」——
// 所以逐条滚日志:每条一行,成功绿字、失败红字带原因,跑完给汇总。
export interface BatchLogLine { text: string; ok?: boolean | null; at?: string }

export const BatchLogModal: React.FC<{
  title: string; lines: BatchLogLine[]; running: boolean;
  total?: number; succeeded?: number; failed?: number; onClose: () => void;
  /** 这一步在做什么(要点清单);没有就不显示说明块 */
  about?: string[];
}> = ({ title, lines, running, total, succeeded, failed, onClose, about }) => {
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => { const el = boxRef.current; if (el) el.scrollTop = el.scrollHeight; }, [lines.length]);
  return (
    <Modal title={title} wide onClose={onClose}
      footer={<Button variant="outline" onClick={onClose} disabled={running}>
        {running ? '处理中…' : '关闭'}</Button>}>
      <div className="space-y-3">
        {!!about?.length && (
          <div className="rounded-lg bg-slate-50 p-2.5 text-[11px] leading-relaxed text-slate-500">
            <div className="mb-1 font-semibold text-slate-600">这一步做了什么</div>
            <ul className="list-disc space-y-0.5 pl-4">
              {about.map((t, i) => <li key={i}>{t}</li>)}
            </ul>
          </div>
        )}
        <div className="flex items-center gap-3 text-xs">
          {running && <span className="flex items-center gap-1 text-blue-600">
            <Loader2 size={12} className="animate-spin" />处理中…</span>}
          {!running && total != null && <>
            <span className="flex items-center gap-1 text-emerald-600">
              <CheckCircle2 size={12} />成功 {succeeded ?? 0}</span>
            <span className="flex items-center gap-1 text-rose-500">
              <AlertTriangle size={12} />失败 {failed ?? 0}</span>
            <span className="text-slate-400">共 {total} 条</span>
          </>}
        </div>
        <div ref={boxRef} className="max-h-[46vh] space-y-1 overflow-y-auto rounded-lg bg-slate-900 p-3 font-mono text-[11px] custom-scrollbar">
          {lines.length === 0 && <div className="text-slate-500">等待后端返回…</div>}
          {lines.map((l, i) => (
            <div key={i} className={cn('flex gap-2',
              l.ok === false ? 'text-rose-300' : l.ok === true ? 'text-emerald-300' : 'text-slate-300')}>
              <span className="shrink-0 text-slate-500">{l.at || ''}</span>
              <span className="whitespace-pre-wrap break-all">{l.text}</span>
            </div>
          ))}
        </div>
      </div>
    </Modal>
  );
};

// ── 批量操作二次确认 ────────────────────────────────────────────────
// 批量一次动几十条,点错一项就是几十条被改 —— 所以**每一个**批量动作执行前都过这一道:
// 把「对谁做、做几条、这一步到底做什么」摆出来,确认了才发请求。改时间 / 复制是先收参数
// 再确认(参数收错了同样改几十条),所以 summary 里把填进去的参数再列一遍。
export const BatchConfirmModal: React.FC<{
  op: BatchOpKey; count: number;
  summary?: string[];
  onCancel: () => void; onConfirm: () => void;
}> = ({ op, count, summary, onCancel, onConfirm }) => {
  const label = OP_LABEL[op] || '批量操作';
  const danger = op === 'delete';
  return (
    <Modal title={`确认${label}`} onClose={onCancel}
      footer={<>
        <Button variant="outline" onClick={onCancel}>取消</Button>
        <Button className={danger ? 'bg-rose-600 text-white' : 'bg-[#2563eb] text-white'}
          onClick={onConfirm}>{`${danger ? '确认删除' : '确认执行'}(${count} 条)`}</Button>
      </>}>
      <div className="space-y-3 text-[12px]">
        <div className="text-slate-700">将对选中的 <b>{count}</b> 条固定问题执行「{label}」。</div>
        {!!summary?.length && (
          <div className="rounded-lg bg-slate-50 p-2.5 text-[11px] leading-relaxed text-slate-600">
            {summary.map((s, i) => <div key={i}>{s}</div>)}
          </div>
        )}
        <div className="rounded-lg bg-slate-50 p-2.5 text-[11px] leading-relaxed text-slate-500">
          <div className="mb-1 font-semibold text-slate-600">这一步做了什么</div>
          <ul className="list-disc space-y-0.5 pl-4">
            {(OP_ABOUT[op] || []).map((t, i) => <li key={i}>{t}</li>)}
          </ul>
        </div>
        {danger && <div className="text-[11px] text-rose-600">删除后不可恢复;没有权限的行会按条失败,其余照常删除。</div>}
      </div>
    </Modal>
  );
};
