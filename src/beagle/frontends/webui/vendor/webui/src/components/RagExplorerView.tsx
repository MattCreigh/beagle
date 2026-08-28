import React, { useState, useEffect } from 'react';
import { RAGSearchResult } from '../types';
import { Search, Database, GitFork, Sparkles, FileCode, Flame } from 'lucide-react';

export const RagExplorerView: React.FC = () => {
  const [query, setQuery] = useState<string>('execute_dag');
  const [results, setResults] = useState<RAGSearchResult[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [compactionRatio, setCompactionRatio] = useState<string>('4.2x (76% savings)');

  const fetchSearchResults = async (searchQuery: string) => {
    setLoading(true);
    try {
      const res = await fetch(`/api/rag/search?q=${encodeURIComponent(searchQuery)}`);
      const data = await res.json();
      setResults(data.results || []);
      if (data.compactionRatio) setCompactionRatio(data.compactionRatio);
    } catch (err) {
      console.error('Failed to query RAG index', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchSearchResults(query);
  }, []);

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    fetchSearchResults(query);
  };

  return (
    <div className="space-y-6">
      {/* Header Banner */}
      <div className="bg-white/[0.02] border border-white/[0.06] p-5 rounded-3xl shadow-xl backdrop-blur-xl">
        <h2 className="text-base font-bold text-white flex items-center gap-2 tracking-tight">
          <Flame className="w-4 h-4 text-orange-500" />
          Hybrid RAG: AST Code Graph & Vector Grounding
        </h2>
        <p className="text-xs text-zinc-400 mt-1 max-w-3xl leading-relaxed">
          Combines semantic vector embeddings (LanceDB) with relational AST caller graphs (Kùzu) to ground agent contexts deterministically without hallucinations.
        </p>
      </div>

      {/* Search & Compaction Stats */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <div className="lg:col-span-3">
          <form onSubmit={handleSearchSubmit} className="relative">
            <Search className="w-3.5 h-3.5 text-zinc-400 absolute left-3.5 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              id="input-rag-search"
              placeholder="Search AST symbols, functions, or semantic concepts (e.g. 'drain_rings', 'meter_token_spend')..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="w-full bg-white/[0.02] border border-white/[0.06] pl-10 pr-24 py-2.5 rounded-2xl text-xs text-zinc-100 placeholder-zinc-400 focus:outline-none focus:border-orange-500 font-mono"
            />
            <button
              type="submit"
              id="btn-rag-submit"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 px-3 py-1.5 bg-gradient-to-r from-orange-600 to-amber-600 hover:from-orange-500 hover:to-amber-500 text-white rounded-xl text-xs font-semibold transition-all shadow-md shadow-orange-500/20 cursor-pointer"
            >
              {loading ? 'Searching...' : 'Traverse'}
            </button>
          </form>
        </div>

        <div className="bg-white/[0.02] border border-white/[0.06] p-3 rounded-2xl flex items-center justify-between shadow-sm backdrop-blur-xl">
          <div>
            <span className="text-[9px] uppercase font-mono text-zinc-400 block">Context Compaction</span>
            <span className="text-xs font-bold text-orange-400 font-mono">{compactionRatio}</span>
          </div>
          <Sparkles className="w-4 h-4 text-orange-400" />
        </div>
      </div>

      {/* Results List */}
      <div className="space-y-3.5">
        <div className="flex items-center justify-between text-xs font-mono text-zinc-400 px-1">
          <span>{results.length} Grounded Context Nodes</span>
          <span>Index: LanceDB (Vector) + Kùzu (AST)</span>
        </div>

        {results.map((res) => (
          <div
            key={res.id}
            id={`rag-result-${res.id}`}
            className="bg-white/[0.02] border border-white/[0.06] hover:border-white/[0.12] rounded-3xl p-5 space-y-3 shadow-xl backdrop-blur-xl transition-all"
          >
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-white/[0.06] pb-2.5">
              <div className="flex items-center gap-2">
                <FileCode className="w-4 h-4 text-orange-400 shrink-0" />
                <span className="text-xs font-bold text-zinc-100 font-mono">{res.filePath}</span>
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-white/[0.04] text-orange-300 border border-orange-500/30 font-mono">
                  {res.symbol}
                </span>
              </div>
              <div className="flex items-center gap-2 text-xs font-mono">
                <span className="text-zinc-400 text-[11px]">Relevance:</span>
                <span className="text-emerald-400 font-semibold text-[11px]">{(res.relevanceScore * 100).toFixed(0)}%</span>
                <span className="text-[9px] px-1.5 py-0.5 rounded-md bg-white/[0.04] text-zinc-300 uppercase border border-white/[0.06]">
                  {res.type.replace('_', ' ')}
                </span>
              </div>
            </div>

            {/* Code Preview snippet */}
            <div className="bg-[#09090d] rounded-2xl p-3.5 border border-white/[0.06] font-mono text-[11px] text-zinc-200 overflow-x-auto leading-relaxed shadow-inner">
              <pre>
                <code>{res.previewCode}</code>
              </pre>
            </div>

            {/* Caller Hierarchy */}
            {res.callerHierarchy && res.callerHierarchy.length > 0 && (
              <div className="flex items-center gap-2 text-[10px] font-mono text-zinc-400 pt-0.5">
                <GitFork className="w-3.5 h-3.5 text-orange-400 shrink-0" />
                <span>Call Hierarchy:</span>
                <span className="text-zinc-300">{res.callerHierarchy.join(' ➔ ')}</span>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
};
