/**
 * 工具中心弹窗路由的纯逻辑(不依赖 react-router,便于单测)。见 lib/modalNav.ts。
 */

/** 以弹窗打开的路由:四类资源的详情 / 编辑页 */
export const MODAL_ROUTE = /^\/tools\/(tool|mcp|skill|agent)\/(detail|edit)$/;

export const isModalRoute = (pathname: string) => MODAL_ROUTE.test(pathname);

type To = string | { pathname?: string };

const pathOf = (to: To) => (typeof to === 'string' ? to.split('?')[0].split('#')[0] : to.pathname || '');

/**
 * 弹窗里发起一次跳转,该怎么处理:
 * - stay:目标是另一个详情/编辑页 → 留在弹窗里;
 * - close:目标就是打开弹窗的那个列表(保存后、点返回)→ 关闭弹窗;
 * - leave:别的页面(分发中心、评测……)→ 按原样整页跳转。
 */
export function modalNavAction(to: To, backgroundPath: string): 'stay' | 'close' | 'leave' {
  const path = pathOf(to);
  if (isModalRoute(path)) return 'stay';
  if (path === backgroundPath) return 'close';
  return 'leave';
}

/** 卡片「最近修改」时间:今年内「09-21 14:30」,跨年带年份,无效值给「—」 */
export function fmtUpdatedAt(v: string | null | undefined, now = new Date()): string {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  const md = `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}-${md}`;
}
