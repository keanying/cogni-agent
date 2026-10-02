
import React from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { 
  LayoutDashboard, 
  Terminal, 
  Wrench, 
  Database, 
  ShieldCheck, 
  Settings,
  Cpu,
  Boxes,
  Zap,
  Bot,
  Globe,
  Binary,
  Layers,
  ListTodo,
  CheckCircle,
  BarChart3,
  FileCode,
  Workflow,
  ChevronRight,
  ChevronDown,
  Share2,
  Smartphone,
  ListChecks,
  Mountain,
  BookOpen,
} from 'lucide-react';
import { useAppStore } from '../../store/useAppStore';
import { cn } from '../../lib/utils';
import { motion, AnimatePresence } from 'framer-motion';

const navItems = [
  { name: '工作台', path: '/', icon: LayoutDashboard },
  { name: '操练台', path: '/console', icon: Terminal },
  { 
    name: '工具中心', 
    path: '/tools', 
    icon: Wrench,
    children: [
      { name: 'Tool', path: '/tools/tool', icon: Cpu },
      { name: 'MCP', path: '/tools/mcp', icon: Boxes },
      { name: 'Skill', path: '/tools/skill', icon: Zap },
      { name: 'Agent', path: '/tools/agent', icon: Bot },
      { name: 'Quick BI', path: '/tools/quickbi', icon: BarChart3 },
      { name: '分发中心', path: '/tools/agent/distribution', icon: Share2 },
    ]
  },
  { 
    name: '知识中心', 
    path: '/knowledge', 
    icon: Database,
    children: [
      { name: '数据库知识', path: '/knowledge/database', icon: Database },
      { name: '网络知识', path: '/knowledge/network', icon: Globe },
      { name: '向量知识', path: '/knowledge/vector', icon: Binary },
      // 维度标签:管理 public_problem_dimension_label(主题→维度→指标),APP 常问问题/快捷问题的分类来源
      { name: '维度标签', path: '/knowledge/dimension-labels', icon: Layers },
      // APP 运营收口到知识中心:快捷问题本质上就是「预置好的提问 + 口径说明」,
      // 属于知识配置而不是独立业务板块。**路由路径保持 /app-ops/* 不变** ——
      // 改路径会让已有书签失效,而且后端权限策略是按路径外的 menu key 判定的,
      // 没必要为了菜单归属去动路由。
      { name: '快捷问题', path: '/app-ops/questions', icon: ListChecks },
      { name: '景区配置', path: '/app-ops/scenic-config', icon: Mountain },
    ]
  },
  {
    name: '审计中心',
    path: '/audit', 
    icon: ShieldCheck,
    children: [
      { name: '任务中心', path: '/audit/tasks', icon: ListTodo },
      { name: '审批中心', path: '/audit/approval', icon: CheckCircle },
      { name: '评测中心', path: '/audit/evaluation', icon: BarChart3 },
    ]
  },
  { 
    name: '系统中心', 
    path: '/system', 
    icon: Settings,
    adminOnly: true,
    children: [
      // 成员/角色/权限已并入「系统设置」的页签,不再单列菜单 ——
      // 它们和模型、组织一样都是系统级配置,分成两个入口反而让人来回找。
      { name: '系统日志', path: '/system/logs', icon: FileCode },
      { name: '系统设置', path: '/system/settings', icon: Settings },
      { name: '系统功能说明', path: '/system/feature-docs', icon: BookOpen },
    ]
  },
  {
    name: '链路可视化',
    path: '/system/graph-viz',
    icon: Workflow,
  },
];

const Sidebar: React.FC = () => {
  const { isSidebarCollapsed, me, permissions } = useAppStore();
  const currentRole = me?.role || '';
  const location = useLocation();
  // 分组**默认全收起**。五个分组一起展开是 20 多行,一屏放不下,常用的那几项
  // 反而要滚动才够得着;而当前页面所在的分组会在下面按路由自动展开 ——
  // 真正需要展开的那一个不用用户自己点,其余的不占地方。
  const [expandedItems, setExpandedItems] = React.useState<string[]>([]);

  // 跟着路由走:进到某个分组下的页面时把它展开,让人知道自己在哪儿。
  // 只在路径变化时补一次,不去收起别的 —— 用户手动展开的分组不该被路由切换收掉。
  React.useEffect(() => {
    const owner = navItems.find(
      (it: any) => (it.children || []).some((c: any) => location.pathname.startsWith(c.path)));
    if (owner && !expandedItems.includes(owner.name)) {
      setExpandedItems(prev => (prev.includes(owner.name) ? prev : [...prev, owner.name]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  const toggleExpand = (name: string) => {
    setExpandedItems(prev => 
      prev.includes(name) ? prev.filter(i => i !== name) : [...prev, name]
    );
  };

  // 菜单可见性由后端 Casbin 策略决定(GET /auth/permissions),前端不再硬编码角色白名单。
  // 硬编码的问题是它和服务端的判断是两份真相,改权限要同时改两处,迟早漂移——
  // 典型症状就是"菜单看得见、一点就 403"。
  // permissions 拿不到时(接口失败)保守起见只放行不敏感的菜单。
  const MENU_KEY: Record<string, string> = {
    '工作台': 'dashboard', '操练台': 'console', '工具中心': 'tools',
    '知识中心': 'knowledge', '审计中心': 'audit', '系统中心': 'system',
    '链路可视化': 'graph_viz',
  };
  const isVisible = (item: any) => {
    const key = MENU_KEY[item.name];
    if (!key) return true;
    // 接口失败时拿不到策略,对集团管理员及以上才有的菜单保守隐藏(与后端策略同口径)。
    if (!permissions) return key !== 'system' && key !== 'graph_viz';
    return permissions.menus[key] !== false;
  };

  return (
    <aside className={cn(
      "h-full bg-[#001529] border-r border-[#000c17] transition-all duration-300 flex flex-col z-20 shadow-xl",
      isSidebarCollapsed ? "w-16" : "w-[240px]"
    )}>
      <div className={cn(
        "h-16 flex items-center px-4 shrink-0 border-b border-[#000c17] bg-gradient-to-r from-[#001529] to-[#002140]",
        !isSidebarCollapsed && "bg-gradient-primary"
      )}>
        <div className="w-8 h-8 rounded-lg bg-white/20 backdrop-blur-sm flex items-center justify-center shrink-0 shadow-lg">
          <Zap className="w-5 h-5 text-white" />
        </div>
        {!isSidebarCollapsed && (
          <motion.span 
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            className="ml-3 font-bold text-lg tracking-tight whitespace-nowrap overflow-hidden text-white"
          >
            数字大脑
          </motion.span>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto custom-scrollbar py-4 px-2 space-y-1">
        {navItems.filter(isVisible).map((item) => {
          const isActive = location.pathname === item.path || (item.children && location.pathname.startsWith(item.path));
          const isExpanded = expandedItems.includes(item.name);

          return (
            <div key={item.name} className="space-y-1">
              {item.children ? (
                <>
                  <button
                    onClick={() => toggleExpand(item.name)}
                    className={cn(
                      "w-full flex items-center px-3 py-2.5 rounded-xl transition-all duration-200 group relative",
                      isActive ? "bg-white/10 text-white" : "text-slate-400 hover:text-white hover:bg-white/5"
                    )}
                  >
                    <item.icon size={20} className={cn("shrink-0", isActive ? "text-[#1677ff]" : "group-hover:text-white")} />
                    {!isSidebarCollapsed && (
                      <>
                        <span className="ml-3 text-sm font-medium flex-1 text-left">{item.name}</span>
                        <ChevronDown 
                          size={14} 
                          className={cn("transition-transform duration-200", isExpanded ? "rotate-0" : "-rotate-90")} 
                        />
                      </>
                    )}
                    {isActive && !isSidebarCollapsed && (
                      <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 bg-[#1677ff] rounded-r-full" />
                    )}
                  </button>
                  <AnimatePresence>
                    {!isSidebarCollapsed && isExpanded && (
                      <motion.div 
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="overflow-hidden space-y-1"
                      >
                        {item.children.map((child) => (
                          <NavLink
                            key={child.path}
                            to={child.path}
                            end
                            className={({ isActive }) => cn(
                              "flex items-center pl-10 pr-4 py-2 rounded-lg transition-all duration-200 text-sm",
                              isActive 
                                ? "bg-[#1677ff] text-white shadow-md shadow-[#1677ff]/20" 
                                : "text-slate-400 hover:text-white hover:bg-white/5"
                            )}
                          >
                            <child.icon size={16} className="mr-3" />
                            <span>{child.name}</span>
                          </NavLink>
                        ))}
                      </motion.div>
                    )}
                  </AnimatePresence>
                </>
              ) : (
                <NavLink
                  to={item.path}
                  className={({ isActive }) => cn(
                    "flex items-center px-3 py-2.5 rounded-xl transition-all duration-200 group relative",
                    isActive ? "bg-[#1677ff] text-white shadow-lg shadow-[#1677ff]/20" : "text-slate-400 hover:text-white hover:bg-white/5"
                  )}
                >
                  <item.icon size={20} className="shrink-0" />
                  {!isSidebarCollapsed && (
                    <span className="ml-3 text-sm font-medium">{item.name}</span>
                  )}
                  {isActive && !isSidebarCollapsed && (
                    <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 bg-white rounded-r-full" />
                  )}
                </NavLink>
              )}
            </div>
          );
        })}
      </nav>

      <div className="p-4 border-t border-[#000c17]">
        <div className={cn(
          "bg-white/5 rounded-xl p-3 flex items-center",
          isSidebarCollapsed ? "justify-center" : "space-x-3"
        )}>
          <div className="w-8 h-8 rounded-full bg-gradient-to-br from-indigo-500 to-purple-500 shrink-0 border border-white/20 flex items-center justify-center text-white text-xs font-bold">
            {me?.name?.[0] || '?'}
          </div>
          {!isSidebarCollapsed && (
            <div className="overflow-hidden">
              <p className="text-xs font-bold text-white truncate">{me?.name || '未登录'}</p>
              <p className="text-[10px] text-slate-500 truncate">{currentRole}</p>
            </div>
          )}
        </div>
      </div>
    </aside>
  );
};

export default Sidebar;
