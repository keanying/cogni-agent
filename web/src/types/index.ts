
export type Role = 'super_admin' | 'group_admin' | 'group_viewer' | 'scenic_operator';

export interface User {
  id: string;
  name: string;
  role: Role;
  avatar: string;
  scenicId?: string;
}

export interface Stats {
  todayTasks: number;
  activeAgents: number;
  tokenConsumption: number;
  successRate: number;
  tasksChange: number;
  agentsChange: number;
  tokensChange: number;
  successChange: number;
}

export interface ApprovalItem {
  id: string;
  name: string;
  initiator: string;
  riskLevel: 'low' | 'medium' | 'high';
  time: string;
  type: string;
  status: 'pending' | 'approved' | 'rejected';
  details?: any;
}

export interface ExecutionStep {
  id: string;
  name: string;
  status: 'waiting' | 'running' | 'success' | 'error';
  duration?: string;
  details?: any;
  input?: any;
  output?: any;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  time: string;
  steps?: ExecutionStep[];
  tokens?: {
    prompt: number;
    completion: number;
    cost: number;
    duration?: string;
  };
}

export interface Session {
  id: string;
  title: string;
  type: 'Agent' | 'Skill' | 'Tool' | 'MCP';
  targetId: string;
  updatedAt: string;
}

export interface Task {
  id: string;
  name: string;
  type: 'Skill' | 'Agent' | 'Tool' | 'MCP';
  status: 'running' | 'succeeded' | 'failed' | 'cancelled';
  initiator: string;
  scenicId: string;
  duration: string;
  time: string;
  tokens: number;
  timeline?: Array<{
    step: string;
    status: string;
    duration: string;
    details?: string;
  }>;
}

export interface Tool {
  id: string;
  name: string;
  description: string;
  type: 'HTTP' | 'Database' | 'Python';
  scope: 'public' | 'group' | 'scenic' | 'private';
  securityLevel: 'low' | 'medium' | 'high';
  isLocked: boolean;
  isEnabled: boolean;
  config: any;
  version: string;
}

export interface MCPServer {
  id: string;
  name: string;
  description: string;
  url: string;
  status: 'connected' | 'auth_failed' | 'timeout';
  toolsCount: number;
}

export interface Skill {
  id: string;
  name: string;
  description: string;
  mode: 'L0' | 'L1' | 'L2';
  isEnabled: boolean;
  toolsCount: number;
  prompt: string;
  version: string;
}

export interface Agent {
  id: string;
  name: string;
  avatar: string;
  description: string;
  personality: string;
  tone: string;
  openingMessage: string;
  skills: string[];
  qpsLimit: number;
  dailyLimit: number;
  scope: string;
  isEnabled: boolean;
}

export interface DataSource {
  id: string;
  name: string;
  type: string;
  scope: string;
  isEnabled: boolean;
  config: any;
}

export interface VectorKB {
  id: string;
  name: string;
  description: string;
  documentsCount: number;
  dimension: number;
  status: 'ready' | 'indexing' | 'error';
  scope: string;
}

export interface LogTrace {
  traceId: string;
  module: string;
  target: string;
  status: number;
  duration: string;
  time: string;
  steps?: any[];
}
