import { create } from 'zustand';
import type { Me, Permissions } from '../lib/api';

interface AppState {
  me: Me | null;
  permissions: Permissions | null;
  isSidebarCollapsed: boolean;
  setMe: (me: Me | null) => void;
  setPermissions: (p: Permissions | null) => void;
  toggleSidebar: () => void;
}

/**
 * 原模板这里是一个"身份模拟切换器"(setRole 直接改前端展示角色,不涉及任何真实鉴权)。
 * 真实系统里角色是登录时由后端签发的 JWT 决定的,不是前端能切换的东西——已替换为
 * 由 /auth/me 加载的真实用户信息;TopBar 里的"身份模拟"下拉菜单相应改成了真实的
 * 用户信息 + 退出登录。
 */
const SIDEBAR_KEY = 'cogni.sidebar.collapsed';

/**
 * 侧边菜单默认**收起**:大部分页面是宽表(固定问题、运行记录、数据资产),
 * 展开的菜单占掉 240px,表格右侧的操作列就被挤出屏幕 —— 而那一列正是最常点的。
 *
 * 但只是默认:展开过就记住,下次进来还是展开的。读写都包了 try —— 隐私模式下
 * localStorage 会直接抛,抛出来的话整个 store 都初始化不了,页面白屏。
 */
function initialCollapsed(): boolean {
  try {
    const saved = localStorage.getItem(SIDEBAR_KEY);
    if (saved !== null) return saved === '1';
  } catch { /* 隐私模式 / 禁用了站点数据:按默认值走 */ }
  return true;
}

export const useAppStore = create<AppState>((set) => ({
  me: null,
  permissions: null,
  isSidebarCollapsed: initialCollapsed(),
  setMe: (me) => set({ me }),
  setPermissions: (permissions) => set({ permissions }),
  toggleSidebar: () => set((state) => {
    const next = !state.isSidebarCollapsed;
    try { localStorage.setItem(SIDEBAR_KEY, next ? '1' : '0'); } catch { /* 同上 */ }
    return { isSidebarCollapsed: next };
  }),
}));
