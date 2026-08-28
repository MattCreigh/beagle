import React from 'react';
import { AgentPersona } from '../types';
import { Shield, Cpu, Activity, Lock, Terminal, Flame } from 'lucide-react';

interface AgentRosterViewProps {
  agents: AgentPersona[];
}

export const AgentRosterView: React.FC<AgentRosterViewProps> = ({ agents }) => {
  return (
    <div className="space-y-6">
      {/* Overview Banner */}
      <div className="bg-white/[0.02] border border-white/[0.06] p-5 rounded-3xl shadow-xl backdrop-blur-xl">
        <h2 className="text-base font-bold text-white flex items-center gap-2 tracking-tight">
          <Flame className="w-4 h-4 text-orange-500" />
          Autonomous Agent Roster & Ring Coordination
        </h2>
        <p className="text-xs text-zinc-400 mt-1 max-w-3xl leading-relaxed">
          Beagle coordinates specialized agents via low-latency memory ring buffers with hardware-level zero-trust boundaries and deterministic role specialization.
        </p>
      </div>

      {/* Agents Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {agents.map((agent) => (
          <div
            key={agent.id}
            id={`agent-card-${agent.id}`}
            className="bg-white/[0.02] border border-white/[0.06] hover:border-orange-500/40 rounded-3xl p-5 space-y-3.5 transition-all duration-200 shadow-xl backdrop-blur-xl group"
          >
            {/* Top row */}
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-3">
                <div className="text-2xl p-2 rounded-2xl bg-white/[0.04] border border-white/[0.06] shadow-inner group-hover:scale-105 transition-transform">
                  {agent.avatar}
                </div>
                <div>
                  <h3 className="text-sm font-bold text-white tracking-tight">{agent.name}</h3>
                  <p className="text-[10px] text-orange-400 font-mono">{agent.role}</p>
                </div>
              </div>

              <span className="flex items-center gap-1 text-[9px] font-mono px-2 py-0.5 rounded-full bg-emerald-950/60 border border-emerald-500/30 text-emerald-300 font-medium">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                {agent.health.toUpperCase()}
              </span>
            </div>

            <p className="text-xs text-zinc-300 leading-relaxed min-h-[36px]">
              {agent.specialty}
            </p>

            {/* Details */}
            <div className="space-y-1.5 pt-3 border-t border-white/[0.06] font-mono text-[10px]">
              <div className="flex items-center justify-between text-zinc-400">
                <span className="flex items-center gap-1.5 text-zinc-400">
                  <Terminal className="w-3 h-3 text-zinc-400" /> Default Engine
                </span>
                <span className="text-zinc-200 bg-white/[0.04] px-1.5 py-0.5 rounded-md border border-white/[0.06]">
                  {agent.defaultModel}
                </span>
              </div>

              <div className="flex items-center justify-between text-zinc-400">
                <span className="flex items-center gap-1.5 text-zinc-400">
                  <Lock className="w-3 h-3 text-orange-400" /> Sandbox Isolation
                </span>
                <span className="text-emerald-300 font-medium">{agent.isolation}</span>
              </div>

              <div className="flex items-center justify-between text-zinc-400">
                <span className="flex items-center gap-1.5 text-zinc-400">
                  <Activity className="w-3 h-3 text-amber-400" /> Ring Buffer Latency
                </span>
                <span className="text-zinc-300">4096B (100μs wait)</span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};
