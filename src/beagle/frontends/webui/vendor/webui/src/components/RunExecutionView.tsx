import React, { useState } from 'react';
import { WorkflowRun, WorkflowNode } from '../types';
import { DagVisualizer } from './DagVisualizer';
import {
  Play,
  Square,
  CheckCircle2,
  AlertOctagon,
  Clock,
  Terminal,
  ShieldCheck,
  Receipt,
  FileCode2,
  UserCheck,
  Flame,
  Zap,
} from 'lucide-react';

interface RunExecutionViewProps {
  runs: WorkflowRun[];
  activeRunId: string | null;
  onSelectRun: (runId: string) => void;
  onRunAction: (runId: string, action: 'step' | 'approve' | 'abort', note?: string) => void;
}

export const RunExecutionView: React.FC<RunExecutionViewProps> = ({
  runs,
  activeRunId,
  onSelectRun,
  onRunAction,
}) => {
  const currentRun = runs.find((r) => r.id === activeRunId) || runs[0];
  const [selectedNode, setSelectedNode] = useState<WorkflowNode | null>(null);
  const [approvalNote, setApprovalNote] = useState<string>('');
  const [logFilter, setLogFilter] = useState<'ALL' | 'SECURITY' | 'COST' | 'INFO'>('ALL');

  if (!currentRun) {
    return (
      <div className="bg-white/[0.02] border border-white/[0.06] rounded-3xl p-12 text-center shadow-xl backdrop-blur-xl">
        <Terminal className="w-10 h-10 text-zinc-600 mx-auto mb-3" />
        <h3 className="text-base font-semibold text-zinc-200">No Workflow Runs Found</h3>
        <p className="text-xs text-zinc-400 mt-1 max-w-sm mx-auto leading-relaxed">
          Launch an autonomous DAG workflow from the DAGs or Agent Chat tab to begin execution tracking.
        </p>
      </div>
    );
  }

  const waitingApprovalNode = currentRun.nodes.find((n) => n.status === 'waiting_approval');

  const filteredLogs = currentRun.logs.filter((log) => {
    if (logFilter === 'ALL') return true;
    return log.level === logFilter;
  });

  const getLogBadge = (level: string) => {
    switch (level) {
      case 'SECURITY':
        return (
          <span className="text-[9px] px-1.5 py-0.5 rounded-md bg-orange-950/70 text-orange-300 border border-orange-500/40 font-mono font-medium">
            SEC
          </span>
        );
      case 'COST':
        return (
          <span className="text-[9px] px-1.5 py-0.5 rounded-md bg-amber-950/70 text-amber-300 border border-amber-500/40 font-mono font-medium">
            COST
          </span>
        );
      case 'WARN':
        return (
          <span className="text-[9px] px-1.5 py-0.5 rounded-md bg-rose-950/70 text-rose-300 border border-rose-500/40 font-mono font-medium">
            WARN
          </span>
        );
      default:
        return (
          <span className="text-[9px] px-1.5 py-0.5 rounded-md bg-white/[0.04] text-zinc-400 border border-white/[0.06] font-mono">
            INFO
          </span>
        );
    }
  };

  return (
    <div className="space-y-6">
      {/* Top Header & Run Switcher */}
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4 bg-white/[0.02] p-5 rounded-3xl border border-white/[0.06] shadow-xl backdrop-blur-xl">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2.5">
            <span className="font-mono text-[11px] text-orange-400 bg-white/[0.04] px-2 py-0.5 rounded-md border border-orange-500/25">
              {currentRun.id}
            </span>
            <h2 className="text-base font-bold text-white tracking-tight">{currentRun.workflowName}</h2>
            {currentRun.status === 'completed' && (
              <span className="text-[10px] px-2.5 py-0.5 rounded-full bg-emerald-950/60 border border-emerald-500/30 text-emerald-300 font-mono flex items-center gap-1 font-medium">
                <CheckCircle2 className="w-3 h-3" /> COMPLETED
              </span>
            )}
            {currentRun.status === 'running' && (
              <span className="text-[10px] px-2.5 py-0.5 rounded-full bg-orange-950/60 border border-orange-500/50 text-orange-300 font-mono flex items-center gap-1 animate-pulse font-medium">
                <Clock className="w-3 h-3" /> EXECUTING DAG
              </span>
            )}
            {currentRun.status === 'paused_hitl' && (
              <span className="text-[10px] px-2.5 py-0.5 rounded-full bg-amber-950/60 border border-amber-500/40 text-amber-300 font-mono flex items-center gap-1 animate-bounce font-medium">
                <UserCheck className="w-3 h-3" /> HITL GATE PENDING
              </span>
            )}
          </div>
          <p className="text-xs text-zinc-400">
            <strong className="text-zinc-300 font-medium">Goal:</strong> {currentRun.goal}
          </p>
        </div>

        {/* Action buttons & Run Selector */}
        <div className="flex flex-wrap items-center gap-2.5">
          <select
            id="select-run-history"
            value={currentRun.id}
            onChange={(e) => onSelectRun(e.target.value)}
            className="bg-white/[0.03] border border-white/[0.08] text-zinc-200 text-xs rounded-xl px-3 py-2 focus:outline-none focus:border-orange-500 font-mono cursor-pointer"
          >
            {runs.map((r) => (
              <option key={r.id} value={r.id} className="bg-zinc-900 text-white">
                {r.id} - {r.workflowName.substring(0, 22)}... ({r.status})
              </option>
            ))}
          </select>

          {currentRun.status === 'running' && (
            <button
              id="btn-step-run"
              onClick={() => onRunAction(currentRun.id, 'step')}
              className="px-3.5 py-2 bg-gradient-to-r from-orange-600 to-amber-600 hover:from-orange-500 hover:to-amber-500 text-white rounded-xl text-xs font-semibold flex items-center gap-1.5 shadow-md shadow-orange-500/20 cursor-pointer transition-all"
            >
              <Play className="w-3.5 h-3.5 fill-white" />
              <span>Step Next Node</span>
            </button>
          )}

          {currentRun.status === 'running' && (
            <button
              id="btn-abort-run"
              onClick={() => onRunAction(currentRun.id, 'abort', 'User requested immediate stop')}
              className="px-3 py-2 bg-rose-950/40 hover:bg-rose-900/60 text-rose-300 border border-rose-500/30 rounded-xl text-xs font-medium flex items-center gap-1.5 transition-colors cursor-pointer"
            >
              <Square className="w-3.5 h-3.5 fill-rose-300" />
              <span>Hard Stop</span>
            </button>
          )}
        </div>
      </div>

      {/* Progress & Spend Metrics Bar */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white/[0.02] border border-white/[0.06] p-4.5 rounded-2xl shadow-sm backdrop-blur-xl">
          <span className="text-[10px] text-zinc-400 uppercase font-mono tracking-wider block mb-1">
            DAG Pipeline Progress
          </span>
          <div className="flex items-end justify-between mb-2">
            <span className="text-base font-bold text-white font-mono">
              Step {currentRun.currentStepIndex} of {currentRun.totalSteps}
            </span>
            <span className="text-xs text-orange-400 font-mono font-semibold">
              {Math.round((currentRun.currentStepIndex / currentRun.totalSteps) * 100)}%
            </span>
          </div>
          <div className="w-full bg-black/60 rounded-full h-1.5 overflow-hidden border border-white/[0.06]">
            <div
              className="bg-gradient-to-r from-orange-500 to-amber-500 h-full transition-all duration-300 rounded-full"
              style={{ width: `${(currentRun.currentStepIndex / currentRun.totalSteps) * 100}%` }}
            ></div>
          </div>
        </div>

        <div className="bg-white/[0.02] border border-white/[0.06] p-4.5 rounded-2xl shadow-sm backdrop-blur-xl">
          <span className="text-[10px] text-zinc-400 uppercase font-mono tracking-wider block mb-1">
            Hard Budget Governance
          </span>
          <div className="flex items-end justify-between mb-2">
            <span className="text-base font-bold text-orange-400 font-mono">
              ${currentRun.spentBudgetUsd.toFixed(2)} / ${currentRun.budgetLimitUsd.toFixed(2)}
            </span>
            <span className="text-xs text-emerald-400 font-mono font-medium">
              {currentRun.spentBudgetUsd <= currentRun.budgetLimitUsd ? 'PASS' : 'EXCEEDED'}
            </span>
          </div>
          <div className="w-full bg-black/60 rounded-full h-1.5 overflow-hidden border border-white/[0.06]">
            <div
              className="bg-orange-500 h-full transition-all duration-300 rounded-full"
              style={{ width: `${Math.min(100, (currentRun.spentBudgetUsd / currentRun.budgetLimitUsd) * 100)}%` }}
            ></div>
          </div>
        </div>

        <div className="bg-white/[0.02] border border-white/[0.06] p-4.5 rounded-2xl shadow-sm backdrop-blur-xl">
          <span className="text-[10px] text-zinc-400 uppercase font-mono tracking-wider block mb-1">
            Total Tokens Metered
          </span>
          <div className="flex items-end justify-between mb-1.5">
            <span className="text-base font-bold text-white font-mono">
              {currentRun.totalTokens.toLocaleString()} tokens
            </span>
            <span className="text-[11px] text-orange-400 font-mono">
              Compaction: 4.2x
            </span>
          </div>
          <p className="text-[11px] text-zinc-400">
            Across {currentRun.nodes.length} isolated agent microVM contexts
          </p>
        </div>
      </div>

      {/* HITL Modal Banner if waiting approval */}
      {waitingApprovalNode && (
        <div
          id="hitl-approval-banner"
          className="bg-amber-950/25 border border-amber-500/50 rounded-2xl p-5 shadow-xl shadow-amber-500/5 space-y-3.5 backdrop-blur-xl"
        >
          <div className="flex items-start gap-3">
            <AlertOctagon className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
            <div className="space-y-0.5 flex-1">
              <h3 className="text-sm font-semibold text-amber-200">
                Human-in-the-Loop (HITL) Gate: Operator Approval Required
              </h3>
              <p className="text-xs text-zinc-300">
                Node <strong className="text-white font-mono">[{waitingApprovalNode.name}]</strong> requires operator sign-off before proceeding.
              </p>
            </div>
          </div>

          <div className="bg-black/60 border border-white/[0.06] p-3 rounded-xl space-y-1.5 text-xs font-mono">
            <div className="flex items-center justify-between text-zinc-400 text-[11px]">
              <span>Agent: {waitingApprovalNode.assignedAgent}</span>
              <span>Model: {waitingApprovalNode.model}</span>
            </div>
            <div className="text-zinc-300 text-xs">
              Directive: {waitingApprovalNode.promptTemplate}
            </div>
          </div>

          <div className="flex flex-col sm:flex-row items-center gap-2.5">
            <input
              type="text"
              id="input-hitl-note"
              placeholder="Operator sign-off remarks..."
              value={approvalNote}
              onChange={(e) => setApprovalNote(e.target.value)}
              className="flex-1 bg-black/80 border border-white/[0.08] px-3 py-2 rounded-xl text-xs text-zinc-200 focus:outline-none focus:border-amber-500 font-sans"
            />
            <div className="flex items-center gap-2 w-full sm:w-auto">
              <button
                id="btn-hitl-approve"
                onClick={() => onRunAction(currentRun.id, 'approve', approvalNote)}
                className="flex-1 sm:flex-initial px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded-xl text-xs font-semibold flex items-center justify-center gap-1.5 transition-colors cursor-pointer"
              >
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>Approve & Step</span>
              </button>
              <button
                id="btn-hitl-reject"
                onClick={() => onRunAction(currentRun.id, 'abort', 'Operator rejected gate approval')}
                className="flex-1 sm:flex-initial px-3.5 py-2 bg-white/[0.04] hover:bg-white/[0.08] text-zinc-300 border border-white/[0.08] rounded-xl text-xs font-medium transition-colors cursor-pointer"
              >
                Reject
              </button>
            </div>
          </div>
        </div>
      )}

      {/* DAG Visualizer */}
      <DagVisualizer
        nodes={currentRun.nodes}
        activeNodeId={selectedNode?.id}
        onSelectNode={(node) => setSelectedNode(node)}
      />

      {/* Selected Node Inspector or Final Synthesis */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Left Column: Synthesis Report or Node Inspector */}
        <div className="space-y-4">
          {currentRun.status === 'completed' && currentRun.finalSynthesis ? (
            <div className="bg-white/[0.02] border border-white/[0.06] rounded-3xl p-5 space-y-4 shadow-xl backdrop-blur-xl">
              <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
                <h3 className="text-sm font-bold text-white flex items-center gap-2">
                  <ShieldCheck className="w-4 h-4 text-orange-400" />
                  CVCP Adversarial Synthesis Report
                </h3>
                <span className="px-2.5 py-0.5 rounded-full bg-emerald-950/60 border border-emerald-500/30 text-emerald-300 font-mono text-[10px] font-semibold">
                  VERDICT: {currentRun.finalSynthesis.adversarialAuditVerdict}
                </span>
              </div>

              <p className="text-xs text-zinc-300 leading-relaxed">
                {currentRun.finalSynthesis.summary}
              </p>

              <div className="space-y-2">
                <span className="text-[10px] font-mono text-zinc-400 uppercase tracking-wider block font-medium">
                  Verified Invariants & Findings:
                </span>
                <ul className="space-y-1.5 text-xs text-zinc-200">
                  {currentRun.finalSynthesis.verifiedFindings.map((finding, idx) => (
                    <li key={idx} className="flex items-start gap-2">
                      <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0 mt-0.5" />
                      <span>{finding}</span>
                    </li>
                  ))}
                </ul>
              </div>

              {/* Itemized Cost Receipt */}
              <div className="bg-black/50 border border-white/[0.06] rounded-2xl p-3.5 space-y-2">
                <div className="flex items-center justify-between text-xs font-mono text-zinc-300 border-b border-white/[0.06] pb-1.5">
                  <span className="flex items-center gap-1.5">
                    <Receipt className="w-3.5 h-3.5 text-orange-400" />
                    Itemized Cost Receipt
                  </span>
                  <span className="text-emerald-400 font-medium">
                    Saved {currentRun.finalSynthesis.costReceipt.savedPercentage}%
                  </span>
                </div>
                <div className="space-y-1 text-[10px] font-mono text-zinc-400">
                  {currentRun.finalSynthesis.costReceipt.tokenBreakdown.map((item, i) => (
                    <div key={i} className="flex items-center justify-between">
                      <span>{item.model} ({item.tokens.toLocaleString()} tok)</span>
                      <span className="text-zinc-200">${item.cost.toFixed(3)}</span>
                    </div>
                  ))}
                  <div className="pt-1.5 border-t border-white/[0.06] flex items-center justify-between text-xs font-bold text-zinc-200">
                    <span>Total Cost Charged</span>
                    <span className="text-orange-400 font-bold">
                      ${currentRun.finalSynthesis.costReceipt.actualSpend.toFixed(2)} USD
                    </span>
                  </div>
                </div>
              </div>
            </div>
          ) : selectedNode ? (
            <div className="bg-white/[0.02] border border-white/[0.06] rounded-3xl p-5 space-y-3.5 shadow-xl backdrop-blur-xl">
              <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
                <div>
                  <span className="text-[9px] font-mono text-zinc-400 uppercase tracking-wider block">Node Inspector</span>
                  <h3 className="text-sm font-bold text-white tracking-tight">{selectedNode.name}</h3>
                </div>
                <span className="font-mono text-[11px] text-orange-400 bg-white/[0.04] px-2 py-0.5 rounded-md border border-orange-500/25">
                  {selectedNode.assignedAgent}
                </span>
              </div>

              <div className="space-y-2 text-xs">
                <div>
                  <span className="text-zinc-400 font-mono text-[10px] block">Prompt Directive:</span>
                  <p className="text-zinc-300 mt-0.5 bg-black/50 p-2.5 rounded-xl border border-white/[0.06] font-mono text-[11px]">
                    {selectedNode.promptTemplate}
                  </p>
                </div>

                {selectedNode.output && (
                  <div>
                    <span className="text-zinc-400 font-mono text-[10px] block">Node Output Artifact:</span>
                    <p className="text-zinc-200 mt-0.5 bg-black/50 p-2.5 rounded-xl border border-white/[0.06] font-mono text-[11px]">
                      {selectedNode.output}
                    </p>
                  </div>
                )}

                <div className="grid grid-cols-3 gap-2 pt-2 text-[10px] font-mono">
                  <div className="bg-black/50 p-2 rounded-xl border border-white/[0.06]">
                    <span className="text-zinc-400 block">Model</span>
                    <span className="text-zinc-200 font-medium">{selectedNode.model}</span>
                  </div>
                  <div className="bg-black/50 p-2 rounded-xl border border-white/[0.06]">
                    <span className="text-zinc-400 block">Tokens</span>
                    <span className="text-zinc-200 font-medium">{selectedNode.tokensUsed}</span>
                  </div>
                  <div className="bg-black/50 p-2 rounded-xl border border-white/[0.06]">
                    <span className="text-zinc-400 block">Spend</span>
                    <span className="text-orange-400 font-bold">${selectedNode.costUsd.toFixed(2)}</span>
                  </div>
                </div>
              </div>
            </div>
          ) : (
            <div className="bg-white/[0.02] border border-white/[0.06] rounded-3xl p-6 text-center space-y-2 shadow-xl backdrop-blur-xl">
              <FileCode2 className="w-8 h-8 text-zinc-600 mx-auto" />
              <h4 className="text-xs font-semibold text-zinc-300">Click any DAG node above to inspect</h4>
              <p className="text-[11px] text-zinc-400">
                View agent prompts, model token meters, and output artifacts.
              </p>
            </div>
          )}
        </div>

        {/* Right Column: Live Logs Terminal */}
        <div className="bg-black/80 border border-white/[0.06] rounded-3xl overflow-hidden flex flex-col h-[420px] shadow-2xl backdrop-blur-xl">
          {/* Terminal Title Bar */}
          <div className="bg-[#09090d] px-4 py-2.5 border-b border-white/[0.06] flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Terminal className="w-3.5 h-3.5 text-orange-400" />
              <span className="text-xs font-mono font-bold text-zinc-200">
                Beacon Event Stream
              </span>
            </div>

            {/* Filter buttons */}
            <div className="flex items-center gap-1 font-mono text-[9px]">
              {(['ALL', 'SECURITY', 'COST', 'INFO'] as const).map((filter) => (
                <button
                  key={filter}
                  id={`log-filter-${filter}`}
                  onClick={() => setLogFilter(filter)}
                  className={`px-2 py-0.5 rounded-md transition-colors cursor-pointer ${
                    logFilter === filter
                      ? 'bg-orange-600 text-white font-semibold'
                      : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.04]'
                  }`}
                >
                  {filter}
                </button>
              ))}
            </div>
          </div>

          {/* Logs scroll container */}
          <div className="p-3.5 font-mono text-[11px] space-y-2 overflow-y-auto flex-1">
            {filteredLogs.map((log) => (
              <div key={log.id} className="flex items-start gap-2 leading-relaxed">
                <span className="text-zinc-600 select-none text-[9px] pt-0.5">
                  {new Date(log.timestamp).toLocaleTimeString()}
                </span>
                {getLogBadge(log.level)}
                <span className="text-orange-400/80 select-none text-[10px] pt-0.5 font-medium">
                  [{log.agent}]:
                </span>
                <span className="text-zinc-300 flex-1">{log.message}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};
