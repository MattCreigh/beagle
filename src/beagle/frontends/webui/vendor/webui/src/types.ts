export type WorkflowNodeStatus = 'pending' | 'running' | 'completed' | 'failed' | 'waiting_approval' | 'skipped';

export interface WorkflowNode {
  id: string;
  name: string;
  type: 'plan' | 'search_rag' | 'execute_sandbox' | 'cvcp_review' | 'synthesize' | 'approval_gate';
  assignedAgent: string;
  model: string;
  dependencies: string[];
  status: WorkflowNodeStatus;
  promptTemplate: string;
  costUsd: number;
  tokensUsed: number;
  durationMs: number;
  output?: string;
  reviewNotes?: string[];
  requiresApproval?: boolean;
}

export interface WorkflowDefinition {
  id: string;
  name: string;
  description: string;
  category: 'audit' | 'feature' | 'security' | 'triage' | 'refactor';
  maxBudgetUsd: number;
  estimatedTokens: number;
  isolationLevel: 'microvm' | 'subprocess';
  nodes: WorkflowNode[];
  createdDate: string;
  author: string;
}

export interface LogEntry {
  id: string;
  timestamp: string;
  level: 'INFO' | 'WARN' | 'DEBUG' | 'SECURITY' | 'COST';
  nodeId?: string;
  agent: string;
  message: string;
  payload?: Record<string, unknown>;
}

export interface WorkflowRun {
  id: string;
  workflowId: string;
  workflowName: string;
  goal: string;
  status: 'running' | 'completed' | 'failed' | 'paused_hitl';
  startTime: string;
  endTime?: string;
  currentStepIndex: number;
  totalSteps: number;
  spentBudgetUsd: number;
  budgetLimitUsd: number;
  totalTokens: number;
  nodes: WorkflowNode[];
  logs: LogEntry[];
  finalSynthesis?: {
    summary: string;
    verifiedFindings: string[];
    adversarialAuditVerdict: 'APPROVED' | 'REJECTED' | 'CONDITIONAL';
    costReceipt: {
      budgetAllocated: number;
      actualSpend: number;
      savedPercentage: number;
      tokenBreakdown: { model: string; tokens: number; cost: number }[];
    };
    modifiedFiles: string[];
  };
}

export interface AgentPersona {
  id: string;
  name: string;
  role: string;
  avatar: string;
  defaultModel: string;
  specialty: string;
  isolation: string;
  activeTasks: number;
  health: 'healthy' | 'busy' | 'standby';
}

export interface RAGSearchResult {
  id: string;
  filePath: string;
  type: 'ast_node' | 'vector_semantic' | 'call_graph';
  relevanceScore: number;
  symbol: string;
  previewCode: string;
  callerHierarchy?: string[];
}

export interface SystemStatus {
  beaconStatus: 'ONLINE' | 'STANDBY';
  activeRings: number;
  sandboxEngine: 'Firecracker MicroVM (/dev/kvm)' | 'Deny-by-Default Subprocess';
  governanceMode: 'HARD_STOP' | 'ALERT_ONLY';
  contextCompactionRatio: string;
  totalSpendToday: number;
  budgetCeiling: number;
  activeRunsCount: number;
}

export interface ChatToolInvocation {
  name: string;
  status: 'running' | 'completed' | 'failed';
  inputSnippet?: string;
  outputSnippet?: string;
  durationMs?: number;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  agentId: string;
  agentName: string;
  agentAvatar: string;
  content: string;
  timestamp: string;
  model: string;
  thinkingProcess?: string;
  toolInvocations?: ChatToolInvocation[];
  suggestedWorkflow?: {
    id: string;
    name: string;
    goal: string;
    nodesCount: number;
    estimatedCost: number;
  };
  tokensUsed?: number;
  costUsd?: number;
}

export interface ChatSession {
  id: string;
  title: string;
  agentId: string;
  model: string;
  messages: ChatMessage[];
  totalTokens: number;
  totalCostUsd: number;
  createdAt: string;
}

