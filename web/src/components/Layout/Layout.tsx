
import React from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import Sidebar from './Sidebar';
import TopBar from './TopBar';
import { useAppStore } from '../../store/useAppStore';
import { cn } from '../../lib/utils';

const Layout: React.FC = () => {
  const { isSidebarCollapsed } = useAppStore();
  const location = useLocation();

  return (
    <div className="flex h-screen bg-[#f8fafc] overflow-hidden">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <TopBar />
        <main className="flex-1 overflow-y-auto custom-scrollbar relative">
          {/* 路由切换不套 AnimatePresence(mode=wait)+key 动画：StrictMode 下会导致
              目标组件 double-mount（两次挂载、两次 loading 闪烁、请求翻倍）。
              直接渲染 Outlet，切换即渲染，无重复挂载。 */}
          <div className={cn(
            "min-h-full",
            location.pathname !== '/console' && "p-6"
          )}>
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
};

export default Layout;
