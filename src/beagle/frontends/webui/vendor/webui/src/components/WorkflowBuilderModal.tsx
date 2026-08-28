import React, { useState } from 'react';
import { WorkflowDefinition, WorkflowNode } from '../types';
import { X, Plus, Trash2, Layers, Flame } from 'lucide-react';

interface WorkflowBuilderModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSave: (workflow: Partial<WorkflowDefinition>) => void;
}

export const WorkflowBuilderModal: React.FC<WorkflowBuilderModalProps> = ({
  isOpen,
  onClose,
  onSave,
}) => {
  const [name, setName] = useState<string>('');
  const [description, setDescription] = useState<string>('');
  const [category, setCategory] = useState<'audit' | 'feature' | 'security' | 'triage' | 'refactor'>('audit');
  const [maxBudgetUsd, setMaxBudgetUsd] = useState<number>(1.5);
  const [isolationLevel, setIsolationLevel] = useState<'microvm' | 'subprocess'>('microvm');

  const [nodes, setNodes] = useState<WorkflowNode[]>([
    {
      id: 'node-c1-plan',
      name: 'Deterministic Scope & Plan',
      type: 'plan',
      assignedAgent: 'Architect Planner',
      model: 'Gemini 3.1 Pro',
      dependencies: [],
      status: 'pending',
      promptTemplate: 'Decompose user requirements into actionable zero-trust DAG nodes.',
      costUsd: 0.15,
      tokensUsed: 4000,
      durationMs: 1200,
    },
    {
      id: 'node-c2-exec',
      name: 'Sandbox Code Implementation',
      type: 'execute_sandbox',
      assignedAgent: 'Goose Sandbox Worker',
      model: 'Gemini 3.7 Flash',
      dependencies: ['node-c1-plan'],
      status: 'pending',
      promptTemplate: 'Execute code generation and run unit test suite in Firecracker microVM.',
      costUsd: 0.45,
      tokensUsed: 15000,
      durationMs: 3200,
    },
    {
      id: 'node-c3-cvcp',
      name: 'CVCP Adversarial Review',
      type: 'cvcp_review',
      assignedAgent: 'CVCP Critic A & B',
      model: 'Dual Model Cross-Check',
      dependencies: ['node-c2-exec'],
      status: 'pending',
      promptTemplate: 'Cross-verify implementation against security invariants and edge-case attacks.',
      costUsd: 0.25,
      tokensUsed: 8000,
      durationMs: 2000,
    },
    {
      id: 'node-c4-synth',
      name: 'Final Synthesis & Verification',
      type: 'synthesize',
      assignedAgent: 'Beacon Coordinator',
      model: 'Gemini 3.7 Flash',
      dependencies: ['node-c3-cvcp'],
      status: 'pending',
      promptTemplate: 'Synthesize verified patch report and itemize cost receipt.',
      costUsd: 0.05,
      tokensUsed: 1500,
      durationMs: 500,
    },
  ]);

  if (!isOpen) return null;

  const handleAddNode = () => {
    const newId = `node-c${nodes.length + 1}-${Date.now().toString(36).slice(-4)}`;
    const lastNode = nodes[nodes.length - 1];

    const newNode: WorkflowNode = {
      id: newId,
      name: 'New Pipeline Step',
      type: 'execute_sandbox',
      assignedAgent: 'Goose Sandbox Worker',
      model: 'Gemini 3.7 Flash',
      dependencies: lastNode ? [lastNode.id] : [],
      status: 'pending',
      promptTemplate: 'Execute assigned task in zero-trust isolation container.',
      costUsd: 0.20,
      tokensUsed: 6000,
      durationMs: 1500,
    };

    setNodes([...nodes, newNode]);
  };

  const handleRemoveNode = (index: number) => {
    if (nodes.length <= 1) return;
    setNodes(nodes.filter((_, i) => i !== index));
  };

  const handleUpdateNode = (index: number, fields: Partial<WorkflowNode>) => {
    const updated = [...nodes];
    updated[index] = { ...updated[index], ...fields };
    setNodes(updated);
  };

  const handleFormSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    onSave({
      name,
      description: description || 'Custom Beagle DAG Workflow',
      category,
      maxBudgetUsd,
      isolationLevel,
      nodes,
    });
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-xl flex items-center justify-center p-4 overflow-y-auto">
      <div className="bg-[#0b0b10] border border-white/[0.08] rounded-3xl w-full max-w-3xl overflow-hidden shadow-2xl space-y-4 my-8">
        {/* Header */}
        <div className="px-6 py-4 bg-white/[0.02] border-b border-white/[0.06] flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <Flame className="w-4 h-4 text-orange-500" />
            <h3 className="text-sm font-bold text-white tracking-tight">Create Custom DAG Workflow</h3>
          </div>
          <button
            id="btn-close-builder"
            onClick={onClose}
            className="p-1 rounded-xl text-zinc-400 hover:text-white hover:bg-white/[0.06] transition-colors cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleFormSubmit} className="p-6 space-y-5">
          {/* Metadata Section */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-1.5 md:col-span-2">
              <label className="text-[11px] font-medium text-zinc-300 font-mono">Workflow Name</label>
              <input
                type="text"
                id="input-wf-name"
                required
                placeholder="e.g., Fast Security Gate & Code Fixer"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full bg-black/60 border border-white/[0.08] px-3 py-2 rounded-xl text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-orange-500 font-sans"
              />
            </div>

            <div className="space-y-1.5 md:col-span-2">
              <label className="text-[11px] font-medium text-zinc-300 font-mono">Description</label>
              <textarea
                id="input-wf-desc"
                rows={2}
                placeholder="Describe the end-to-end goal of this multi-agent pipeline..."
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full bg-black/60 border border-white/[0.08] px-3 py-2 rounded-xl text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-orange-500 font-sans"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-[11px] font-medium text-zinc-300 font-mono">Category</label>
              <select
                id="select-wf-category"
                value={category}
                onChange={(e) => setCategory(e.target.value as any)}
                className="w-full bg-black/60 border border-white/[0.08] px-3 py-2 rounded-xl text-xs text-zinc-200 focus:outline-none focus:border-orange-500 font-mono cursor-pointer"
              >
                <option value="audit">Audit & CVCP</option>
                <option value="feature">Feature / Code Implementation</option>
                <option value="security">Security Gate</option>
                <option value="refactor">Autonomous Refactoring</option>
                <option value="triage">Bug Triage & Root Cause</option>
              </select>
            </div>

            <div className="space-y-1.5">
              <label className="text-[11px] font-medium text-zinc-300 font-mono">Max Hard Budget ($ USD)</label>
              <input
                type="number"
                id="input-wf-budget"
                step="0.10"
                min="0.10"
                max="20.00"
                value={maxBudgetUsd}
                onChange={(e) => setMaxBudgetUsd(Number(e.target.value))}
                className="w-full bg-black/60 border border-white/[0.08] px-3 py-2 rounded-xl text-xs text-orange-400 font-mono focus:outline-none focus:border-orange-500"
              />
            </div>
          </div>

          {/* Nodes Section */}
          <div className="space-y-2.5">
            <div className="flex items-center justify-between">
              <h4 className="text-[11px] font-semibold text-zinc-300 font-mono uppercase tracking-wider">
                DAG Nodes & Execution Sequence ({nodes.length})
              </h4>
              <button
                type="button"
                id="btn-add-dag-node"
                onClick={handleAddNode}
                className="px-2.5 py-1 bg-white/[0.04] hover:bg-white/[0.08] border border-white/[0.08] text-orange-400 text-xs rounded-xl font-mono flex items-center gap-1.5 transition-colors cursor-pointer"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>Add Node</span>
              </button>
            </div>

            <div className="space-y-2.5 max-h-60 overflow-y-auto pr-1">
              {nodes.map((node, index) => (
                <div
                  key={node.id}
                  className="bg-black/50 border border-white/[0.06] rounded-2xl p-3 space-y-2.5"
                >
                  <div className="flex items-center justify-between gap-2.5">
                    <span className="font-mono text-[10px] text-zinc-500">0{index + 1}</span>
                    <input
                      type="text"
                      value={node.name}
                      onChange={(e) => handleUpdateNode(index, { name: e.target.value })}
                      className="flex-1 bg-white/[0.03] border border-white/[0.08] px-2.5 py-1 rounded-xl text-xs text-zinc-100 font-medium focus:outline-none focus:border-orange-500 font-sans"
                    />
                    <select
                      value={node.type}
                      onChange={(e) => handleUpdateNode(index, { type: e.target.value as any })}
                      className="bg-white/[0.03] border border-white/[0.08] text-zinc-300 text-xs rounded-xl px-2 py-1 font-mono cursor-pointer"
                    >
                      <option value="plan">Plan</option>
                      <option value="search_rag">Hybrid RAG</option>
                      <option value="execute_sandbox">Sandbox Worker</option>
                      <option value="cvcp_review">CVCP Review</option>
                      <option value="approval_gate">HITL Gate</option>
                      <option value="synthesize">Synthesizer</option>
                    </select>

                    <button
                      type="button"
                      onClick={() => handleRemoveNode(index)}
                      className="text-zinc-500 hover:text-rose-400 p-1 transition-colors cursor-pointer"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>

                  <input
                    type="text"
                    value={node.promptTemplate}
                    onChange={(e) => handleUpdateNode(index, { promptTemplate: e.target.value })}
                    placeholder="Node prompt directive..."
                    className="w-full bg-white/[0.02] border border-white/[0.06] px-2.5 py-1 rounded-xl text-xs text-zinc-300 font-mono focus:outline-none focus:border-orange-500"
                  />
                </div>
              ))}
            </div>
          </div>

          {/* Modal Footer Actions */}
          <div className="flex items-center justify-end gap-2.5 pt-3 border-t border-white/[0.06]">
            <button
              type="button"
              id="btn-cancel-builder"
              onClick={onClose}
              className="px-4 py-2 bg-white/[0.04] hover:bg-white/[0.08] text-zinc-300 rounded-xl text-xs font-medium transition-colors cursor-pointer"
            >
              Cancel
            </button>
            <button
              type="submit"
              id="btn-save-custom-dag"
              className="px-5 py-2 bg-gradient-to-r from-orange-600 to-amber-600 hover:from-orange-500 hover:to-amber-500 text-white rounded-xl text-xs font-semibold shadow-md shadow-orange-500/20 transition-all cursor-pointer"
            >
              Save Workflow
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
