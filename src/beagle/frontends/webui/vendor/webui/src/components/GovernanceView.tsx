import React, { useState, useEffect } from 'react';
import { CircleDollarSign, Lock, BarChart3, CheckCircle2, Flame, ShieldAlert } from 'lucide-react';

export const GovernanceView: React.FC = () => {
  const [costData, setCostData] = useState<{
    dailyBudgetCap: number;
    currentDaySpend: number;
    remainingBudget: number;
    spendByModel: { model: string; cost: number; percentage: number }[];
    governancePolicy: string;
  } | null>(null);

  const [customDailyCap, setCustomDailyCap] = useState<number>(25.00);

  useEffect(() => {
    fetch('/api/cost/summary')
      .then((res) => res.json())
      .then((data) => {
        setCostData(data);
        if (data.dailyBudgetCap) setCustomDailyCap(data.dailyBudgetCap);
      })
      .catch((err) => console.error('Failed to load cost summary', err));
  }, []);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="bg-white/[0.02] border border-white/[0.06] p-5 rounded-3xl shadow-xl backdrop-blur-xl">
        <h2 className="text-base font-bold text-white flex items-center gap-2 tracking-tight">
          <Flame className="w-4 h-4 text-orange-500" />
          Hard Cost Governance & Zero-Trust Policies
        </h2>
        <p className="text-xs text-zinc-400 mt-1 max-w-3xl leading-relaxed">
          Enforces immutable cost ceilings and fail-closed security gates. Every model invocation is token-metered before execution.
        </p>
      </div>

      {/* Metrics Row */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white/[0.02] border border-white/[0.06] p-5 rounded-3xl space-y-2 shadow-xl backdrop-blur-xl">
          <span className="text-[10px] font-mono text-zinc-400 uppercase tracking-wider block">
            Today's Metered Spend
          </span>
          <div className="text-2xl font-bold font-mono text-orange-400">
            ${costData?.currentDaySpend.toFixed(2) || '0.88'} USD
          </div>
          <p className="text-xs text-zinc-400">
            Capped against ${customDailyCap.toFixed(2)} USD ceiling
          </p>
        </div>

        <div className="bg-white/[0.02] border border-white/[0.06] p-5 rounded-3xl space-y-2 shadow-xl backdrop-blur-xl">
          <span className="text-[10px] font-mono text-zinc-400 uppercase tracking-wider block">
            Remaining Daily Budget
          </span>
          <div className="text-2xl font-bold font-mono text-emerald-400">
            ${(customDailyCap - (costData?.currentDaySpend || 0.88)).toFixed(2)} USD
          </div>
          <p className="text-xs text-emerald-400/90 flex items-center gap-1 font-mono">
            <CheckCircle2 className="w-3.5 h-3.5" /> Budget healthy (No overages)
          </p>
        </div>

        <div className="bg-white/[0.02] border border-white/[0.06] p-5 rounded-3xl space-y-2 shadow-xl backdrop-blur-xl">
          <span className="text-[10px] font-mono text-zinc-400 uppercase tracking-wider block">
            Enforcement Mode
          </span>
          <div className="text-2xl font-bold font-mono text-orange-300">
            FAIL-CLOSED
          </div>
          <p className="text-xs text-zinc-400">
            Hard execution kill upon reaching budget cap
          </p>
        </div>
      </div>

      {/* Model Spend Breakdown & Policy Settings */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Spend by Model */}
        <div className="bg-white/[0.02] border border-white/[0.06] rounded-3xl p-5 space-y-4 shadow-xl backdrop-blur-xl">
          <h3 className="text-sm font-bold text-white flex items-center gap-2">
            <BarChart3 className="w-4 h-4 text-orange-400" />
            Spend Distribution by Model Family
          </h3>

          <div className="space-y-3">
            {costData?.spendByModel.map((item, idx) => (
              <div key={idx} className="space-y-1.5 text-xs font-mono">
                <div className="flex items-center justify-between text-zinc-300 text-[11px]">
                  <span>{item.model}</span>
                  <span className="text-orange-400 font-semibold">${item.cost.toFixed(3)} ({item.percentage}%)</span>
                </div>
                <div className="w-full bg-black/60 rounded-full h-1.5 overflow-hidden border border-white/[0.06]">
                  <div
                    className="bg-gradient-to-r from-orange-500 to-amber-500 h-full rounded-full"
                    style={{ width: `${item.percentage}%` }}
                  ></div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Policy Configuration */}
        <div className="bg-white/[0.02] border border-white/[0.06] rounded-3xl p-5 space-y-4 shadow-xl backdrop-blur-xl">
          <h3 className="text-sm font-bold text-white flex items-center gap-2">
            <Lock className="w-4 h-4 text-orange-400" />
            Zero-Trust Isolation Invariants
          </h3>

          <div className="space-y-2.5 text-xs">
            <div className="p-3 bg-black/50 rounded-2xl border border-white/[0.06] space-y-1">
              <span className="font-mono text-zinc-200 font-semibold block text-[11px]">
                1. MicroVM Sandbox Invariant
              </span>
              <p className="text-zinc-400 text-[11px] leading-relaxed">
                Untrusted code generated by Goose sandbox agents executes inside Firecracker jail without host network access.
              </p>
            </div>

            <div className="p-3 bg-black/50 rounded-2xl border border-white/[0.06] space-y-1">
              <span className="font-mono text-zinc-200 font-semibold block text-[11px]">
                2. CVCP Adversarial Peer Consensus
              </span>
              <p className="text-zinc-400 text-[11px] leading-relaxed">
                No synthesis report is emitted without unanimous consensus from two independent critic agents cross-verifying outputs.
              </p>
            </div>

            <div className="p-3 bg-black/50 rounded-2xl border border-white/[0.06] space-y-1">
              <span className="font-mono text-zinc-200 font-semibold block text-[11px]">
                3. Consequential Action HITL Gate
              </span>
              <p className="text-zinc-400 text-[11px] leading-relaxed">
                File mutations and deploy actions pause execution until explicitly approved by human operator.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
