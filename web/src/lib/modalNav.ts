/**
 * 工具中心「查看 / 修改」以弹窗打开(Tool / MCP / Skill / Agent)。
 *
 * 做法是 react-router 的「背景路由」:列表页打开详情时把当前 location 放进
 * `state.background`,App 用背景 location 继续渲染列表,再把详情/编辑页叠在弹窗里。
 * 这样详情/编辑页本身一行不用改 —— 它们照旧从 URL 读 id,地址栏也照旧可复制、可刷新
 * (直接打开链接 / 刷新时没有 background,就按原来的整页显示)。
 *
 * 弹窗里的跳转用 `useModalNavigate` 代替 `useNavigate`:
 * - 跳到另一个详情/编辑页(详情 → 编辑)→ 留在弹窗里,replace 不堆历史;
 * - 跳回列表(保存后、点返回)→ 关闭弹窗,列表按 location.key 变化自动刷新;
 * - 跳到别处(分发中心、评测……)→ 按原样整页跳转。
 * 不在弹窗里时它和 `useNavigate` 完全一样。
 */
import { useCallback } from 'react';
import { useLocation, useNavigate, type Location } from 'react-router-dom';
import { modalNavAction } from './modalNavCore';

// 与 react-router 的 To / NavigateOptions 同形;本地声明,避免依赖其类型导出细节
type To = string | { pathname?: string; search?: string; hash?: string };
type NavigateOptions = { replace?: boolean; state?: any; preventScrollReset?: boolean };

export { MODAL_ROUTE, isModalRoute } from './modalNavCore';

/** 列表页用:以弹窗打开某个详情 / 编辑页 */
export function openInModal(navigate: ReturnType<typeof useNavigate>, location: Location, to: string) {
  navigate(to, { state: { background: location } });
}

/** 在弹窗内外都可用的 navigate:弹窗里跳详情/编辑保持弹窗,跳回列表即关闭 */
export function useModalNavigate() {
  const navigate = useNavigate();
  const location = useLocation();
  const background: Location | undefined = (location.state as any)?.background;
  return useCallback((to: To | number, opts?: NavigateOptions) => {
    if (typeof to === 'number') { navigate(to); return; }
    if (!background) { navigate(to, opts); return; }
    const action = modalNavAction(to, background.pathname);
    if (action === 'stay') {
      navigate(to, { ...opts, replace: true, state: { ...(opts?.state as any), background } });
    } else if (action === 'close') {
      // 回到打开弹窗的那个列表:替换掉弹窗这一条,浏览器后退不会再把弹窗弹回来
      navigate(to, { ...opts, replace: true });
    } else {
      navigate(to, opts);
    }
  }, [navigate, background]);
}
