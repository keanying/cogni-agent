
import React from 'react';
import {
  Menu,
  Search,
  Bell,
  ChevronDown,
  LogOut,
  Shield,
  X
} from 'lucide-react';
import { useAppStore } from '../../store/useAppStore';
import * as api from '../../lib/api';
import { useNavigate } from 'react-router-dom';

const ROLE_LABEL: Record<string, string> = {
  super_admin: '超级管理员', group_admin: '集团管理员', group_developer: '集团开发',
  group_viewer: '集团只读', scenic_admin: '景区管理员', scenic_operator: '景区操作员',
};

const TopBar: React.FC = () => {
  const { isSidebarCollapsed, toggleSidebar, me } = useAppStore();
  const [isMenuOpen, setIsMenuOpen] = React.useState(false);
  const navigate = useNavigate();

  const handleLogout = () => {
    api.logout();
    window.location.reload();
  };

  return (
    <header className="h-[56px] bg-white border-b border-[#e8e8e8] px-4 flex items-center justify-between z-10 shrink-0">
      <div className="flex items-center">
        <button
          onClick={toggleSidebar}
          className="p-2 text-slate-500 hover:text-[#1677ff] transition-colors mr-4"
        >
          {isSidebarCollapsed ? <Menu size={20} /> : <X size={20} />}
        </button>

        <div className="hidden md:flex items-center bg-[#f5f5f5] rounded-lg px-3 py-1.5 border border-[#e8e8e8] w-64">
          <Search size={16} className="text-slate-400 mr-2" />
          <input
            type="text"
            placeholder="搜索资源、任务或日志..."
            className="bg-transparent border-none outline-none text-sm text-[#1f1f1f] w-full placeholder:text-slate-400"
          />
        </div>
      </div>

      <div className="flex items-center space-x-4">
        <button className="p-2 text-slate-500 hover:text-[#1677ff] transition-colors relative">
          <Bell size={20} />
          <span className="absolute top-1.5 right-1.5 w-2 h-2 bg-[#ff4d4f] rounded-full border-2 border-white"></span>
        </button>

        <div className="relative pl-4 border-l border-[#e8e8e8]">
          <button
            onClick={() => setIsMenuOpen(!isMenuOpen)}
            className="flex items-center space-x-3 hover:bg-slate-50 transition-colors px-2 py-1 rounded-lg"
          >
            <div className="text-right hidden sm:block">
              <p className="text-sm font-bold text-[#1f1f1f] leading-tight">{me?.name || '未登录'}</p>
              <p className="text-[10px] text-slate-500 uppercase tracking-wider">{me ? (ROLE_LABEL[me.role] || me.role) : ''}</p>
            </div>
            <div className="w-9 h-9 rounded-full bg-gradient-to-br from-indigo-500 to-purple-500 border border-white/20 flex items-center justify-center text-white text-sm font-bold shrink-0">
              {me?.name?.[0] || '?'}
            </div>
            <ChevronDown size={14} className="text-slate-400" />
          </button>

          {isMenuOpen && (
            <>
              <div className="fixed inset-0 z-20" onClick={() => setIsMenuOpen(false)} />
              <div className="absolute right-0 mt-2 w-56 bg-white border border-[#e8e8e8] rounded-lg shadow-xl z-30 overflow-hidden py-1">
                <div className="px-4 py-3 border-b border-[#f0f0f0]">
                  <p className="text-sm font-bold text-[#1f1f1f]">{me?.name}</p>
                  <p className="text-xs text-slate-400 font-mono mt-0.5">{me?.account}</p>
                  <div className="flex items-center gap-1.5 mt-2">
                    <Shield size={12} className="text-[#1677ff]" />
                    <span className="text-xs text-slate-600">{me ? (ROLE_LABEL[me.role] || me.role) : ''}</span>
                  </div>
                  {me?.scenic_id && <p className="text-[10px] text-slate-400 mt-1">景区：{me.scenic_id}</p>}
                </div>
                <button
                  onClick={handleLogout}
                  className="w-full flex items-center px-4 py-2.5 text-sm text-slate-600 hover:bg-slate-50 transition-colors"
                >
                  <LogOut size={16} className="mr-3" />
                  退出登录
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
};

export default TopBar;
