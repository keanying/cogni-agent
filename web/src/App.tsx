
import React, { useEffect, useState } from 'react';
import { Routes, Route, Navigate, useLocation, type Location } from 'react-router-dom';
import RouteModal from './components/common/RouteModal';
import { isModalRoute } from './lib/modalNav';
import Layout from './components/Layout/Layout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Console from './pages/Console';
import ToolList from './pages/Tools/ToolList';
import ToolNew from './pages/Tools/ToolNew';
import ToolEdit from './pages/Tools/ToolEdit';
import ToolDetail from './pages/Tools/ToolDetail';
import MCPList from './pages/Tools/MCPList';
import MCPNew from './pages/Tools/MCPNew';
import MCPEdit from './pages/Tools/MCPEdit';
import MCPDetail from './pages/Tools/MCPDetail';
import SkillList from './pages/Tools/SkillList';
import SkillNew from './pages/Tools/SkillNew';
import SkillEdit from './pages/Tools/SkillEdit';
import SkillDetail from './pages/Tools/SkillDetail';
import QuestionBank from './pages/AppOps/QuestionBank';
import ScenicConfig from './pages/AppOps/ScenicConfig';
import ScenicConfigDetail from './pages/AppOps/ScenicConfigDetail';
import AgentList from './pages/Tools/AgentList';
import QuickBIPage from './pages/Tools/QuickBI';
import AgentNew from './pages/Tools/AgentNew';
import AgentEdit from './pages/Tools/AgentEdit';
import AgentDetail from './pages/Tools/AgentDetail';
import AgentDistribution from './pages/Tools/AgentDistribution';
import DatabaseKnowledge from './pages/Knowledge/DatabaseKnowledge';
import DatabaseNew from './pages/Knowledge/DatabaseNew';
import DatabaseDetail from './pages/Knowledge/DatabaseDetail';
import NetworkKnowledge from './pages/Knowledge/NetworkKnowledge';
import VectorKnowledge from './pages/Knowledge/VectorKnowledge';
import VectorNew from './pages/Knowledge/VectorNew';
import VectorDetail from './pages/Knowledge/VectorDetail';
import DimensionLabels from './pages/Knowledge/DimensionLabels';
import TaskList from './pages/Audit/TaskList';
import ApprovalCenter from './pages/Audit/ApprovalCenter';
import EvaluationCenter from './pages/Audit/EvaluationCenter';
import EvaluationNew from './pages/Audit/EvaluationNew';
import EvaluationDetail from './pages/Audit/EvaluationDetail';
import SystemLogs from './pages/System/SystemLogs';
import SystemSettings from './pages/System/SystemSettings';
import FeatureDocs from './pages/System/FeatureDocs';
import GraphViz from './pages/System/GraphViz';
import { Toaster } from 'sonner';
import { ConfirmHost } from './components/ui/confirm';
import * as api from './lib/api';
import { useAppStore } from './store/useAppStore';

const App: React.FC = () => {
  const { me, setMe, setPermissions } = useAppStore();
  // 工具中心「查看/修改」弹窗:列表页打开时带 state.background,列表继续按背景渲染、详情叠在弹窗里
  const location = useLocation();
  const bgState: Location | undefined = (location.state as any)?.background;
  const background = bgState && isModalRoute(location.pathname) ? bgState : undefined;
  const [authed, setAuthed] = useState(api.isAuthed());
  const [checking, setChecking] = useState(api.isAuthed());

  // 令牌彻底失效(refresh 也换不回来)时由 api 层回调通知,统一回登录页。
  // 以前每个页面各自 catch 401,结果是有的页面弹 toast、有的静默失败、有的卡住。
  useEffect(() => {
    api.setSessionExpiredHandler(() => setAuthed(false));
    return () => api.setSessionExpiredHandler(null);
  }, []);

  useEffect(() => {
    if (!authed) { setChecking(false); return; }
    Promise.all([api.getMe(), api.getPermissions().catch(() => null)])
      .then(([m, p]) => { setMe(m); setPermissions(p); })
      .catch(() => { api.logout(); setAuthed(false); })
      .finally(() => setChecking(false));
  }, [authed]);

  if (!authed) {
    return (
      <div className="min-h-screen bg-background text-foreground font-sans selection:bg-[#2563eb]/30">
        <Login onSuccess={() => setAuthed(true)} />
        <ConfirmHost />
        <Toaster position="top-right" richColors />
      </div>
    );
  }

  if (checking || !me) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#f8fafc]">
        <div className="text-sm text-slate-400">正在加载...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground font-sans selection:bg-[#2563eb]/30">
      <Routes location={background || location}>
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="console" element={<Console />} />

          <Route path="tools/tool" element={<ToolList />} />
          <Route path="tools/tool/new" element={<ToolNew />} />
          <Route path="tools/tool/edit" element={<ToolEdit />} />
          <Route path="tools/tool/detail" element={<ToolDetail />} />

          <Route path="tools/mcp" element={<MCPList />} />
          <Route path="tools/mcp/new" element={<MCPNew />} />
          <Route path="tools/mcp/edit" element={<MCPEdit />} />
          <Route path="tools/mcp/detail" element={<MCPDetail />} />

          <Route path="tools/skill" element={<SkillList />} />
          <Route path="tools/skill/new" element={<SkillNew />} />
          <Route path="tools/skill/detail" element={<SkillDetail />} />
          <Route path="tools/skill/edit" element={<SkillEdit />} />

          {/* ── 工具中心 · Quick BI(独立模块) ── */}
          <Route path="tools/quickbi" element={<QuickBIPage />} />

          <Route path="tools/agent" element={<AgentList />} />
          <Route path="tools/agent/new" element={<AgentNew />} />
          <Route path="tools/agent/edit" element={<AgentEdit />} />
          <Route path="tools/agent/detail" element={<AgentDetail />} />
          <Route path="tools/agent/distribution" element={<AgentDistribution />} />

          {/* ── APP 运营(19号文档:APP端业务后台,首期固定问题) ── */}
          <Route path="app-ops/questions" element={<QuestionBank />} />
          <Route path="app-ops/scenic-config" element={<ScenicConfig />} />
          <Route path="app-ops/scenic-config/:scenicId" element={<ScenicConfigDetail />} />

          <Route path="knowledge/database" element={<DatabaseKnowledge />} />
          <Route path="knowledge/database/new" element={<DatabaseNew />} />
          <Route path="knowledge/database/detail" element={<DatabaseDetail />} />

          <Route path="knowledge/network" element={<NetworkKnowledge />} />

          <Route path="knowledge/vector" element={<VectorKnowledge />} />
          <Route path="knowledge/vector/new" element={<VectorNew />} />
          <Route path="knowledge/vector/detail" element={<VectorDetail />} />
          <Route path="knowledge/dimension-labels" element={<DimensionLabels />} />

          <Route path="audit/tasks" element={<TaskList />} />
          <Route path="audit/approval" element={<ApprovalCenter />} />

          <Route path="audit/evaluation" element={<EvaluationCenter />} />
          <Route path="audit/evaluation/new" element={<EvaluationNew />} />
          <Route path="audit/evaluation/detail" element={<EvaluationDetail />} />

          {/* 用户管理已并入系统设置(系统成员/系统角色/权限管理三个页签),旧链接直接转过去 */}
          <Route path="user-center" element={<Navigate to="/system/settings?tab=members" replace />} />
          <Route path="system/logs" element={<SystemLogs />} />
          <Route path="system/settings" element={<SystemSettings />} />
          <Route path="system/feature-docs" element={<FeatureDocs />} />
          <Route path="system/graph-viz" element={<GraphViz />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      {background && (
        <Routes>
          <Route path="/tools/tool/detail" element={<RouteModal><ToolDetail /></RouteModal>} />
          <Route path="/tools/tool/edit" element={<RouteModal><ToolEdit /></RouteModal>} />
          <Route path="/tools/mcp/detail" element={<RouteModal><MCPDetail /></RouteModal>} />
          <Route path="/tools/mcp/edit" element={<RouteModal><MCPEdit /></RouteModal>} />
          <Route path="/tools/skill/detail" element={<RouteModal><SkillDetail /></RouteModal>} />
          <Route path="/tools/skill/edit" element={<RouteModal><SkillEdit /></RouteModal>} />
          <Route path="/tools/agent/detail" element={<RouteModal><AgentDetail /></RouteModal>} />
          <Route path="/tools/agent/edit" element={<RouteModal><AgentEdit /></RouteModal>} />
        </Routes>
      )}
      <ConfirmHost />
      <Toaster position="top-right" richColors />
    </div>
  );
};

export default App;
