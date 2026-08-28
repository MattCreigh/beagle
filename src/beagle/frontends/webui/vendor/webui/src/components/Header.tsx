import React from 'react';
import { Shield, Zap, Terminal, Activity, Cpu, CircleDollarSign, MessageSquareCode, Flame, Sparkles } from 'lucide-react';
import { SystemStatus } from '../types';

interface HeaderProps {
  systemStatus: SystemStatus | null;
  activeTab: 'chat' | 'workflows' | 'runs' | 'agents' | 'rag' | 'governance';
  setActiveTab: (tab: 'chat' | 'workflows' | 'runs' | 'agents' | 'rag' | 'governance') => void;
  activeRunsCount: number;
}

export const Header: React.FC<HeaderProps> = ({
  systemStatus,
  activeTab,
  setActiveTab,
  activeRunsCount,
}) => {
  return (
    <header className="border-b border-white/[0.06] bg-[#070709]/80 backdrop-blur-2xl sticky top-0 z-40 transition-all">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16 gap-4">
          {/* Logo & Brand */}
          <div className="flex items-center gap-3 shrink-0">
            <div className="relative group">
              <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-orange-500 via-amber-500 to-orange-600 flex items-center justify-center shadow-lg shadow-orange-500/20 text-white font-bold text-base border border-orange-400/40 transition-transform duration-300 group-hover:scale-105">
                <Flame className="w-4 h-4 text-white" />
              </div>
              <div className="absolute -inset-0.5 bg-orange-500/20 rounded-xl blur-sm -z-10 group-hover:bg-orange-500/40 transition-all" />
            </div>

            <div className="flex flex-col">
              <div className="flex items-center gap-2">
                <span className="font-bold text-base tracking-tight text-white flex items-center gap-1.5">
                  BEAGLE
                </span>
                <span className="text-[10px] px-1.5 py-0.5 rounded-md bg-white/[0.04] text-orange-400 border border-orange-500/25 font-mono font-medium">
                  v1.4.2
                </span>
                <span className="hidden sm:inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-emerald-950/50 text-emerald-300 border border-emerald-500/20 font-mono">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                  BEACON {systemStatus?.beaconStatus || 'ONLINE'}
                </span>
              </div>
              <p className="text-[11px] text-zinc-400 hidden xl:block font-normal">
                Autonomous Multi-Agent Engine • Hardware MicroVM Isolation
              </p>
            </div>
          </div>

          {/* Quick Metrics */}
          <div className="hidden lg:flex items-center gap-3 text-xs">
            <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1.5 rounded-xl">
              <Shield className="w-3.5 h-3.5 text-orange-400" />
              <span className="text-zinc-400 text-[11px]">Sandbox:</span>
              <span className="font-mono text-[11px] text-zinc-200 font-medium">MicroVM</span>
            </div>
            <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1.5 rounded-xl">
              <CircleDollarSign className="w-3.5 h-3.5 text-amber-400" />
              <span className="text-zinc-400 text-[11px]">Spend:</span>
              <span className="font-mono text-[11px] text-orange-400 font-semibold">
                ${systemStatus?.totalSpendToday.toFixed(2) || '0.00'}
              </span>
              <span className="text-zinc-400 font-mono text-[10px]">
                / ${systemStatus?.budgetCeiling.toFixed(2) || '25.00'}
              </span>
            </div>
          </div>

          {/* Segmented Modern Tab Bar */}
          <nav className="flex items-center p-1 rounded-2xl bg-white/[0.03] border border-white/[0.06] backdrop-blur-md">
            <button
              id="tab-btn-chat"
              onClick={() => setActiveTab('chat')}
              className={`px-3 py-1.5 rounded-xl text-xs font-medium transition-all duration-200 flex items-center gap-1.5 cursor-pointer ${
                activeTab === 'chat'
                  ? 'bg-gradient-to-r from-orange-600 to-amber-600 text-white shadow-md shadow-orange-500/25 ring-1 ring-white/15'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.04]'
              }`}
            >
              <MessageSquareCode className="w-3.5 h-3.5" />
              <span>Chat</span>
              <span className="w-1.5 h-1.5 rounded-full bg-orange-300 animate-pulse hidden sm:inline-block"></span>
            </button>

            <button
              id="tab-btn-workflows"
              onClick={() => setActiveTab('workflows')}
              className={`px-3 py-1.5 rounded-xl text-xs font-medium transition-all duration-200 flex items-center gap-1.5 cursor-pointer ${
                activeTab === 'workflows'
                  ? 'bg-gradient-to-r from-orange-600 to-amber-600 text-white shadow-md shadow-orange-500/25 ring-1 ring-white/15'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.04]'
              }`}
            >
              <Terminal className="w-3.5 h-3.5" />
              <span>DAGs</span>
            </button>

            <button
              id="tab-btn-runs"
              onClick={() => setActiveTab('runs')}
              className={`px-3 py-1.5 rounded-xl text-xs font-medium transition-all duration-200 flex items-center gap-1.5 relative cursor-pointer ${
                activeTab === 'runs'
                  ? 'bg-gradient-to-r from-orange-600 to-amber-600 text-white shadow-md shadow-orange-500/25 ring-1 ring-white/15'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.04]'
              }`}
            >
              <Activity className="w-3.5 h-3.5" />
              <span>Execution</span>
              {activeRunsCount > 0 && (
                <span className="w-1.5 h-1.5 rounded-full bg-orange-400 animate-ping"></span>
              )}
            </button>

            <button
              id="tab-btn-agents"
              onClick={() => setActiveTab('agents')}
              className={`px-3 py-1.5 rounded-xl text-xs font-medium transition-all duration-200 flex items-center gap-1.5 cursor-pointer ${
                activeTab === 'agents'
                  ? 'bg-gradient-to-r from-orange-600 to-amber-600 text-white shadow-md shadow-orange-500/25 ring-1 ring-white/15'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.04]'
              }`}
            >
              <Cpu className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Agents</span>
            </button>

            <button
              id="tab-btn-rag"
              onClick={() => setActiveTab('rag')}
              className={`px-3 py-1.5 rounded-xl text-xs font-medium transition-all duration-200 flex items-center gap-1.5 cursor-pointer ${
                activeTab === 'rag'
                  ? 'bg-gradient-to-r from-orange-600 to-amber-600 text-white shadow-md shadow-orange-500/25 ring-1 ring-white/15'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.04]'
              }`}
            >
              <Shield className="w-3.5 h-3.5" />
              <span className="hidden md:inline">RAG AST</span>
            </button>

            <button
              id="tab-btn-governance"
              onClick={() => setActiveTab('governance')}
              className={`px-3 py-1.5 rounded-xl text-xs font-medium transition-all duration-200 flex items-center gap-1.5 cursor-pointer ${
                activeTab === 'governance'
                  ? 'bg-gradient-to-r from-orange-600 to-amber-600 text-white shadow-md shadow-orange-500/25 ring-1 ring-white/15'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-white/[0.04]'
              }`}
            >
              <CircleDollarSign className="w-3.5 h-3.5" />
              <span className="hidden lg:inline">Budget</span>
            </button>
          </nav>
        </div>
      </div>
    </header>
  );
};
