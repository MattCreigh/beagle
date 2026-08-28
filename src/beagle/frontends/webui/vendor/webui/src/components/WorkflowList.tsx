import React, { useState } from 'react';
import { WorkflowDefinition } from '../types';
import { Play, Plus, Shield, Cpu, Clock, Layers, ArrowUpRight, Search, Flame } from 'lucide-react';
import { DagVisualizer } from './DagVisualizer';

interface WorkflowListProps {
  workflows: WorkflowDefinition[];
  onExecute: (workflowId: string, goal: string, budget: number) => void;
  onOpenBuilder: () => void;
}

export const WorkflowList: React.FC<WorkflowListProps> = ({
  workflows,
  onExecute,
  onOpenBuilder,
}) => {
  const [selectedCategory, setSelectedCategory] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [expandedWorkflowId, setExpandedWorkflowId] = useState<string | null>(workflows[0]?.id || null);
  const [customGoal, setCustomGoal] = useState<string>('');
  const [customBudget, setCustomBudget] = useState<number>(1.5);

  const categories = ['all', 'audit', 'feature', 'security'];

  const filteredWorkflows = workflows.filter((w) => {
    const matchesCategory = selectedCategory === 'all' || w.category === selectedCategory;
    const matchesSearch =
      w.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      w.description.toLowerCase().includes(searchQuery.toLowerCase());
    return matchesCategory && matchesSearch;
  });

  const getCategoryBadge = (cat: string) => {
    switch (cat) {
      case 'audit':
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-mono bg-orange-950/50 text-orange-300 border border-orange-500/30 font-medium">
            AUDIT & CVCP
          </span>
        );
      case 'feature':
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-mono bg-amber-950/50 text-amber-300 border border-amber-500/30 font-medium">
            FEATURE / CODE
          </span>
        );
      case 'security':
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-mono bg-rose-950/50 text-rose-300 border border-rose-500/30 font-medium">
            SECURITY GATE
          </span>
        );
      default:
        return (
          <span className="px-2 py-0.5 rounded-full text-[10px] font-mono bg-white/[0.04] text-zinc-300 border border-white/[0.06]">
            {cat.toUpperCase()}
          </span>
        );
    }
  };

  return (
    <div className="space-y-6">
      {/* Action Header */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 bg-white/[0.02] p-5 rounded-3xl border border-white/[0.06] shadow-xl backdrop-blur-xl">
        <div>
          <h2 className="text-base font-bold text-white flex items-center gap-2 tracking-tight">
            <Flame className="w-4 h-4 text-orange-500" />
            Deterministic DAG Workflows
          </h2>
          <p className="text-xs text-zinc-400 mt-1 max-w-2xl leading-relaxed">
            Multi-agent pipelines with topological ordering, zero-trust Firecracker sandbox boundaries, and CVCP adversarial critique.
          </p>
        </div>

        <div className="flex items-center gap-3">
          <button
            id="btn-create-custom-wf"
            onClick={onOpenBuilder}
            className="px-4 py-2 bg-gradient-to-r from-orange-600 to-amber-600 hover:from-orange-500 hover:to-amber-500 text-white rounded-2xl text-xs font-semibold flex items-center gap-2 shadow-md shadow-orange-500/20 transition-all cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>Create Custom DAG</span>
          </button>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className="flex flex-col sm:flex-row items-center justify-between gap-4">
        <div className="flex items-center gap-2 overflow-x-auto w-full sm:w-auto pb-1 sm:pb-0">
          {categories.map((cat) => (
            <button
              key={cat}
              id={`filter-cat-${cat}`}
              onClick={() => setSelectedCategory(cat)}
              className={`px-3 py-1.5 rounded-xl text-xs font-medium capitalize transition-all font-mono cursor-pointer ${
                selectedCategory === cat
                  ? 'bg-white/[0.08] text-orange-400 border border-orange-500/40 shadow-sm'
                  : 'bg-white/[0.02] text-zinc-400 hover:text-zinc-200 border border-white/[0.06]'
              }`}
            >
              {cat}
            </button>
          ))}
        </div>

        <div className="relative w-full sm:w-72">
          <Search className="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            id="input-search-workflows"
            placeholder="Search DAG pipelines..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full bg-white/[0.02] border border-white/[0.06] pl-9 pr-3 py-1.5 rounded-xl text-xs text-zinc-200 placeholder-zinc-400 focus:outline-none focus:border-orange-500 transition-colors font-sans"
          />
        </div>
      </div>

      {/* Workflow Cards */}
      <div className="grid grid-cols-1 gap-4">
        {filteredWorkflows.map((wf) => {
          const isExpanded = expandedWorkflowId === wf.id;

          return (
            <div
              key={wf.id}
              id={`wf-card-${wf.id}`}
              className={`rounded-3xl border transition-all duration-200 overflow-hidden backdrop-blur-xl ${
                isExpanded
                  ? 'border-orange-500/40 bg-white/[0.03] shadow-lg shadow-orange-500/5'
                  : 'border-white/[0.06] bg-white/[0.02] hover:border-white/[0.12]'
              }`}
            >
              {/* Card Header Summary */}
              <div className="p-5">
                <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                  <div className="space-y-1.5">
                    <div className="flex items-center gap-3">
                      <h3 className="text-sm font-bold text-white tracking-tight">{wf.name}</h3>
                      {getCategoryBadge(wf.category)}
                    </div>
                    <p className="text-xs text-zinc-400 line-clamp-2 leading-relaxed">{wf.description}</p>
                  </div>

                  {/* Metadata Chips */}
                  <div className="flex flex-wrap items-center gap-2.5 text-xs font-mono text-zinc-300">
                    <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1 rounded-xl">
                      <span className="text-zinc-400 text-[10px]">Cap:</span>
                      <span className="text-orange-400 font-semibold text-[11px]">${wf.maxBudgetUsd.toFixed(2)} USD</span>
                    </div>
                    <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1 rounded-xl">
                      <Cpu className="w-3 h-3 text-orange-400" />
                      <span className="text-[11px]">{wf.nodes.length} Nodes</span>
                    </div>
                    <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1 rounded-xl">
                      <Shield className="w-3 h-3 text-emerald-400" />
                      <span className="text-[11px]">{wf.isolationLevel === 'microvm' ? 'MicroVM' : 'Subprocess'}</span>
                    </div>

                    <button
                      id={`btn-toggle-expand-${wf.id}`}
                      onClick={() => setExpandedWorkflowId(isExpanded ? null : wf.id)}
                      className="px-3 py-1 bg-white/[0.04] hover:bg-white/[0.08] text-zinc-200 border border-white/[0.06] rounded-xl text-xs font-sans transition-colors cursor-pointer"
                    >
                      {isExpanded ? 'Collapse' : 'Inspect DAG'}
                    </button>
                  </div>
                </div>
              </div>

              {/* Expanded Pipeline Details & Execution Controller */}
              {isExpanded && (
                <div className="border-t border-white/[0.06] p-5 bg-black/40 space-y-4">
                  <DagVisualizer nodes={wf.nodes} />

                  {/* Execute Controls */}
                  <div className="bg-white/[0.02] border border-white/[0.06] rounded-2xl p-4 flex flex-col md:flex-row items-stretch md:items-center justify-between gap-4">
                    <div className="flex-1 space-y-1.5">
                      <label className="text-[11px] font-medium text-zinc-300 block font-mono">
                        Prompt Objective / Task Target
                      </label>
                      <input
                        type="text"
                        id={`input-goal-${wf.id}`}
                        placeholder={`e.g. Audit authentication boundary in auth/jwt.py for token expiration tampering`}
                        value={customGoal}
                        onChange={(e) => setCustomGoal(e.target.value)}
                        className="w-full bg-black/60 border border-white/[0.08] px-3 py-2 rounded-xl text-xs text-zinc-200 placeholder-zinc-400 focus:outline-none focus:border-orange-500 font-sans"
                      />
                    </div>

                    <div className="w-36 space-y-1.5">
                      <label className="text-[11px] font-medium text-zinc-300 block font-mono">
                        Budget Cap (USD)
                      </label>
                      <input
                        type="number"
                        step="0.10"
                        min="0.10"
                        max="10.00"
                        id={`input-budget-${wf.id}`}
                        defaultValue={wf.maxBudgetUsd}
                        onChange={(e) => setCustomBudget(Number(e.target.value))}
                        className="w-full bg-black/60 border border-white/[0.08] px-3 py-2 rounded-xl text-xs text-orange-400 font-mono focus:outline-none focus:border-orange-500"
                      />
                    </div>

                    <div className="flex items-end">
                      <button
                        id={`btn-launch-run-${wf.id}`}
                        onClick={() =>
                          onExecute(
                            wf.id,
                            customGoal || `Run ${wf.name}`,
                            customBudget || wf.maxBudgetUsd
                          )
                        }
                        className="w-full md:w-auto px-5 py-2.5 bg-gradient-to-r from-orange-600 to-amber-600 hover:from-orange-500 hover:to-amber-500 text-white rounded-xl text-xs font-semibold flex items-center justify-center gap-2 shadow-md shadow-orange-500/20 transition-all cursor-pointer"
                      >
                        <Play className="w-3.5 h-3.5 fill-white" />
                        <span>Launch Autonomous Run</span>
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
