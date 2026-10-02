import React, { createContext, useContext, useEffect } from 'react';
import { X } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';

/**
 * 工具中心详情 / 编辑页的弹窗外壳(配合 lib/modalNav 的背景路由)。
 *
 * 注意两点:
 * - 遮罩**不能**加 backdrop-blur / transform:这两个属性会让子元素里 `position: fixed`
 *   的二级弹窗(删除确认、发布确认……)以本弹窗为定位基准,被裁掉或错位;
 * - 关闭 = 回到背景列表(replace,浏览器后退不会再弹回来)。
 */
/**
 * 页面是否正显示在弹窗里。详情页据此切成「顶部固定 + 各栏固定高度、各自滚动」的布局
 * (弹窗高度是定的,整页一起滚的话顶部操作按钮和左侧版本列表都会被滚走)。
 */
const InRouteModal = createContext(false);
export const useInRouteModal = () => useContext(InRouteModal);

const RouteModal: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const bg = (location.state as any)?.background;
  const close = () => navigate(bg ? `${bg.pathname}${bg.search || ''}` : -1 as any, { replace: true });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // 二级弹窗开着时 Esc 交给它自己处理(页面里的 Modal 都是 z-50 及以上的 fixed 层)
      if (e.key !== 'Escape' || document.querySelectorAll('.fixed.inset-0').length > 1) return;
      close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/40 p-4"
         onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}
         data-route-modal>
      <div role="dialog" aria-modal="true"
           className="relative w-full max-w-6xl h-[90vh] rounded-2xl bg-[#f8fafc] shadow-2xl overflow-hidden flex flex-col">
        <button type="button" onClick={close} title="关闭(Esc)"
          className="absolute right-3 top-3 z-20 h-8 w-8 rounded-full bg-white/90 border border-slate-200
                     flex items-center justify-center text-slate-500 hover:text-slate-800 hover:bg-white shadow-sm">
          <X size={16} />
        </button>
        <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar p-6 pr-14">
          <InRouteModal.Provider value={true}>{children}</InRouteModal.Provider>
        </div>
      </div>
    </div>
  );
};

export default RouteModal;
