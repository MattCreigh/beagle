import React, { useState, useEffect } from 'react';
import { Header } from './components/Header';
import { WorkflowList } from './components/WorkflowList';
import { RunExecutionView } from './components/RunExecutionView';
import { AgentRosterView } from './components/AgentRosterView';
import { RagExplorerView } from './components/RagExplorerView';
import { GovernanceView } from './components/GovernanceView';
import { ChatView } from './components/ChatView';
import { WorkflowBuilderModal } from './components/WorkflowBuilderModal';
import { WorkflowDefinition, WorkflowRun, AgentPersona, SystemStatus } from './types';
import { INITIAL_WORKFLOWS, INITIAL_AGENTS, INITIAL_SYSTEM_STATUS } from './data/defaultWorkflows';

export function App() {
  const [activeTab, setActiveTab] = useState<'workflows' | 'runs' | 'agents' | 'rag' | 'governance' | 'chat'>('chat');
  const [workflows, setWorkflows] = useState<WorkflowDefinition[]>(INITIAL_WORKFLOWS);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [agents, setAgents] = useState<AgentPersona[]>(INITIAL_AGENTS);
  const [systemStatus, setSystemStatus] = useState<SystemStatus>(INITIAL_SYSTEM_STATUS);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [isBuilderOpen, setIsBuilderOpen] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(true);

  // Load initial data from Express API
  const refreshData = async () => {
    try {
      const [wfRes, runsRes, agentsRes, statusRes] = await Promise.all([
        fetch('/api/workflows').then((r) => (r.ok ? r.json() : INITIAL_WORKFLOWS)),
        fetch('/api/runs').then((r) => (r.ok ? r.json() : [])),
        fetch('/api/agents/roster').then((r) => (r.ok ? r.json() : INITIAL_AGENTS)),
        fetch('/api/system/status').then((r) => (r.ok ? r.json() : INITIAL_SYSTEM_STATUS)),
      ]);

      setWorkflows(wfRes);
      setRuns(runsRes);
      setAgents(agentsRes);
      setSystemStatus(statusRes);
      if (runsRes.length > 0 && !activeRunId) {
        setActiveRunId(runsRes[0].id);
      }
    } catch (err) {
      console.error('Failed to load Beagle system state', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refreshData();
  }, []);

  // Launch a new run
  const handleExecuteWorkflow = async (workflowId: string, goal: string, budget: number) => {
    try {
      const res = await fetch(`/api/workflows/${workflowId}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ goal, budgetLimitUsd: budget }),
      });

      if (res.ok) {
        const newRun = await res.json();
        setRuns((prev) => [newRun, ...prev]);
        setActiveRunId(newRun.id);
        setActiveTab('runs');
      }
    } catch (err) {
      console.error('Failed to execute workflow', err);
    }
  };

  // Step or action on active run
  const handleRunAction = async (runId: string, action: 'step' | 'approve' | 'abort', note?: string) => {
    try {
      const res = await fetch(`/api/runs/${runId}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, note }),
      });

      if (res.ok) {
        const updatedRun = await res.json();
        setRuns((prev) => prev.map((r) => (r.id === updatedRun.id ? updatedRun : r)));
        // Refresh system spend and status
        fetch('/api/system/status')
          .then((r) => r.json())
          .then((s) => setSystemStatus(s));
      }
    } catch (err) {
      console.error('Failed to apply run action', err);
    }
  };

  // Save custom workflow
  const handleSaveWorkflow = async (newWfData: Partial<WorkflowDefinition>) => {
    try {
      const res = await fetch('/api/workflows', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newWfData),
      });

      if (res.ok) {
        const savedWf = await res.json();
        setWorkflows((prev) => [savedWf, ...prev]);
      }
    } catch (err) {
      console.error('Failed to save workflow', err);
    }
  };

  const activeRunsCount = runs.filter((r) => r.status === 'running' || r.status === 'paused_hitl').length;

  return (
    <div className="min-h-screen bg-[#070709] text-zinc-100 flex flex-col selection:bg-orange-500/30 selection:text-orange-200 relative overflow-x-hidden font-sans">
      {/* Delicate Ambient Radial Glow */}
      <div
        className="pointer-events-none fixed inset-x-0 top-0 h-[480px] bg-[radial-gradient(ellipse_60%_40%_at_50%_-10%,rgba(249,115,22,0.08),rgba(255,255,255,0))] z-0"
        aria-hidden="true"
      />
      <div
        className="pointer-events-none fixed top-1/3 left-1/2 -translate-x-1/2 w-[800px] h-[300px] bg-[radial-gradient(circle,rgba(249,115,22,0.03),transparent_70%)] z-0"
        aria-hidden="true"
      />

      {/* Header */}
      <Header
        systemStatus={systemStatus}
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        activeRunsCount={activeRunsCount}
      />

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6 relative z-10">
        {activeTab === 'chat' && (
          <ChatView
            agents={agents}
            onExecuteWorkflow={handleExecuteWorkflow}
          />
        )}

        {activeTab === 'workflows' && (
          <WorkflowList
            workflows={workflows}
            onExecute={handleExecuteWorkflow}
            onOpenBuilder={() => setIsBuilderOpen(true)}
          />
        )}

        {activeTab === 'runs' && (
          <RunExecutionView
            runs={runs}
            activeRunId={activeRunId}
            onSelectRun={(id) => setActiveRunId(id)}
            onRunAction={handleRunAction}
          />
        )}

        {activeTab === 'agents' && <AgentRosterView agents={agents} />}

        {activeTab === 'rag' && <RagExplorerView />}

        {activeTab === 'governance' && <GovernanceView />}
      </main>

      {/* Custom DAG Builder Modal */}
      <WorkflowBuilderModal
        isOpen={isBuilderOpen}
        onClose={() => setIsBuilderOpen(false)}
        onSave={handleSaveWorkflow}
      />
    </div>
  );
}

export default App;
