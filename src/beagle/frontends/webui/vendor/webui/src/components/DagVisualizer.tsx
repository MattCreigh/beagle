import React from 'react';
import { WorkflowNode } from '../types';
import { CheckCircle2, Clock, PlayCircle, AlertTriangle, ShieldCheck, UserCheck, Flame } from 'lucide-react';

interface DagVisualizerProps {
  nodes: WorkflowNode[];
  activeNodeId?: string;
  onSelectNode?: (node: WorkflowNode) => void;
}

export const DagVisualizer: React.FC<DagVisualizerProps> = ({
  nodes,
  activeNodeId,
  onSelectNode,
}) => {
  const getNodeIcon = (type: WorkflowNode['type'], status: WorkflowNode['status']) => {
    if (status === 'completed') return <CheckCircle2 className="w-4 h-4 text-emerald-400" />;
    if (status === 'running') return <PlayCircle className="w-4 h-4 text-orange-400 animate-spin" />;
    if (status === 'waiting_approval') return <UserCheck className="w-4 h-4 text-amber-400 animate-pulse" />;
    if (status === 'failed') return <AlertTriangle className="w-4 h-4 text-rose-400" />;
    if (type === 'cvcp_review') return <ShieldCheck className="w-4 h-4 text-orange-400" />;
    return <Clock className="w-4 h-4 text-zinc-600" />;
  };

  const getStatusBadge = (status: WorkflowNode['status']) => {
    switch (status) {
      case 'completed':
        return (
          <span className="text-[9px] px-2 py-0.5 rounded-full bg-emerald-950/60 border border-emerald-500/30 text-emerald-300 font-mono font-medium">
            COMPLETE
          </span>
        );
      case 'running':
        return (
          <span className="text-[9px] px-2 py-0.5 rounded-full bg-orange-950/60 border border-orange-500/50 text-orange-300 font-mono font-medium animate-pulse">
            RUNNING
          </span>
        );
      case 'waiting_approval':
        return (
          <span className="text-[9px] px-2 py-0.5 rounded-full bg-amber-950/60 border border-amber-500/40 text-amber-300 font-mono font-semibold animate-pulse">
            HITL GATE
          </span>
        );
      case 'failed':
        return (
          <span className="text-[9px] px-2 py-0.5 rounded-full bg-rose-950/60 border border-rose-500/40 text-rose-300 font-mono">
            FAILED
          </span>
        );
      default:
        return (
          <span className="text-[9px] px-2 py-0.5 rounded-full bg-white/[0.03] border border-white/[0.06] text-zinc-400 font-mono">
            PENDING
          </span>
        );
    }
  };

  return (
    <div className="w-full bg-white/[0.02] border border-white/[0.06] rounded-2xl p-4.5 overflow-x-auto backdrop-blur-xl">
      <div className="flex items-center justify-between mb-4 border-b border-white/[0.06] pb-3">
        <div className="flex items-center gap-2">
          <Flame className="w-4 h-4 text-orange-400" />
          <h4 className="text-xs font-semibold uppercase tracking-wider text-zinc-200 font-mono">
            Deterministic DAG Execution Pipeline
          </h4>
        </div>
        <div className="text-xs text-zinc-400 flex items-center gap-4">
          <span className="flex items-center gap-1.5 font-mono text-[10px]">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> Verified
          </span>
          <span className="flex items-center gap-1.5 font-mono text-[10px]">
            <span className="w-1.5 h-1.5 rounded-full bg-orange-400"></span> Active
          </span>
          <span className="flex items-center gap-1.5 font-mono text-[10px]">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400"></span> HITL Gate
          </span>
        </div>
      </div>

      {/* Nodes visual flow */}
      <div className="flex items-center min-w-max gap-3 py-2 px-1">
        {nodes.map((node, index) => {
          const isSelected = activeNodeId === node.id;
          const isLast = index === nodes.length - 1;

          return (
            <React.Fragment key={node.id}>
              {/* Node Card */}
              <div
                id={`dag-node-${node.id}`}
                onClick={() => onSelectNode && onSelectNode(node)}
                className={`cursor-pointer transition-all duration-200 relative group w-64 rounded-2xl border p-3.5 ${
                  isSelected
                    ? 'border-orange-500/80 bg-white/[0.05] shadow-lg shadow-orange-500/10 ring-1 ring-orange-500/40'
                    : node.status === 'running'
                    ? 'border-orange-500/60 bg-orange-950/20 shadow-sm'
                    : node.status === 'waiting_approval'
                    ? 'border-amber-500/60 bg-amber-950/20 shadow-sm'
                    : 'border-white/[0.06] bg-white/[0.02] hover:border-white/[0.12] hover:bg-white/[0.04]'
                }`}
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="font-mono text-[10px] text-zinc-400 font-medium">
                    Step 0{index + 1}
                  </span>
                  {getStatusBadge(node.status)}
                </div>

                <div className="flex items-start gap-2.5 mb-2.5">
                  <div className="mt-0.5">{getNodeIcon(node.type, node.status)}</div>
                  <div>
                    <h5 className="text-xs font-semibold text-zinc-100 line-clamp-1 group-hover:text-orange-400 transition-colors">
                      {node.name}
                    </h5>
                    <p className="text-[10px] text-zinc-400 font-mono mt-0.5">
                      {node.assignedAgent}
                    </p>
                  </div>
                </div>

                <div className="pt-2 border-t border-white/[0.06] flex items-center justify-between text-[10px] text-zinc-400 font-mono">
                  <span className="text-zinc-300 bg-white/[0.04] px-1.5 py-0.5 rounded-md border border-white/[0.06]">
                    {node.model}
                  </span>
                  <span className="text-orange-400 font-semibold">${node.costUsd.toFixed(2)}</span>
                </div>
              </div>

              {/* Connector Arrow */}
              {!isLast && (
                <div className="flex items-center justify-center text-zinc-600 px-1">
                  <div className="h-[1px] w-6 bg-white/[0.1] relative">
                    <div className="absolute right-0 top-1/2 -translate-y-1/2 w-0 h-0 border-t-3 border-t-transparent border-b-3 border-b-transparent border-l-4 border-l-orange-400/80"></div>
                  </div>
                </div>
              )}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
};
