import React, { useState, useRef, useEffect } from 'react';
import { AgentPersona, ChatMessage } from '../types';
import {
  Send,
  Sparkles,
  Bot,
  Terminal,
  Copy,
  Check,
  ChevronDown,
  ChevronRight,
  Play,
  Trash2,
  Download,
  Flame,
  Zap,
  ArrowUpRight,
  CircleDollarSign,
  Cpu,
  Layers,
  Code2,
  CornerDownLeft,
} from 'lucide-react';

interface ChatViewProps {
  agents: AgentPersona[];
  onExecuteWorkflow: (workflowId: string, goal: string, budget: number) => void;
}

const PROMPT_PRESETS = [
  {
    title: 'Adversarial Security Audit',
    desc: 'Audit JWT validation & token tampering in auth/jwt.py',
    prompt: 'Perform an adversarial security audit on the JWT token verification flow in auth/jwt.py, checking for expiration bypass and signature tampering.',
    agentId: 'cvcp-critic-1',
  },
  {
    title: 'Deterministic DAG Plan',
    desc: 'Decompose task queue into microVM pipeline with $1.20 ceiling',
    prompt: 'Design a deterministic 4-node DAG workflow with microVM isolation and budget ceiling $1.20 USD for async task queue refactoring.',
    agentId: 'planner-agent',
  },
  {
    title: 'Traverse AST Call Graph',
    desc: 'Query Kùzu & LanceDB index for AutonomousOrchestrator',
    prompt: 'Query the AST call hierarchy for the symbol AutonomousOrchestrator.execute_dag and show compact context callers.',
    agentId: 'rag-scout',
  },
  {
    title: 'Hard Budget Governance',
    desc: 'Simulate fail-closed token cutoff and spend metering',
    prompt: 'Explain how Beagle enforces hard budget limits before model calls, and what occurs when a worker exceeds its cost ceiling.',
    agentId: 'beacon-coord',
  },
];

export const ChatView: React.FC<ChatViewProps> = ({ agents, onExecuteWorkflow }) => {
  const [selectedAgentId, setSelectedAgentId] = useState<string>(agents[0]?.id || 'beacon-coord');
  const [selectedModel, setSelectedModel] = useState<string>('gemini-3.7-flash');
  const [input, setInput] = useState<string>('');
  const [loading, setLoading] = useState<boolean>(false);
  const [expandedThinking, setExpandedThinking] = useState<Record<string, boolean>>({});
  const [copiedCodeId, setCopiedCodeId] = useState<string | null>(null);

  const initialMessage: ChatMessage = {
    id: 'msg-welcome-001',
    role: 'assistant',
    agentId: 'beacon-coord',
    agentName: 'Beacon Coordinator',
    agentAvatar: '🧭',
    content: `Welcome to **Beagle Autonomous Engine**. I am the **Beacon Coordinator**, operating with hardware-isolated MicroVM boundaries, deterministic DAG pipelines, and fail-closed token governance.

You can direct inquiries to me or route tasks directly to specialized ring agents:
- 📐 **Architect Planner**: Deterministic topological DAG orchestration
- 🔍 **Code Scout**: Hybrid RAG traversing AST syntax graphs and vector embeddings
- ⚙️ **Goose Sandbox Worker**: Isolated Firecracker execution and automated test runs
- 🛡️ **CVCP Critics (A & B)**: Independent adversarial spec audit and peer consensus

Select a starter directive below or enter a customized prompt to begin.`,
    timestamp: new Date().toISOString(),
    model: 'Gemini 3.7 Flash',
    thinkingProcess: 'Runtime ready. Synced ring memory buffers with 6 attached worker agents. Zero-trust microVM isolation verified.',
    tokensUsed: 420,
    costUsd: 0.0012,
  };

  const [messages, setMessages] = useState<ChatMessage[]>([initialMessage]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const selectedAgent = agents.find((a) => a.id === selectedAgentId) || agents[0];

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, loading]);

  const handleSendMessage = async (customText?: string) => {
    const textToSend = customText || input;
    if (!textToSend.trim() || loading) return;

    const userMessage: ChatMessage = {
      id: `msg-u-${Date.now()}`,
      role: 'user',
      agentId: 'user',
      agentName: 'Operator',
      agentAvatar: '👤',
      content: textToSend,
      timestamp: new Date().toISOString(),
      model: selectedModel,
    };

    setMessages((prev) => [...prev, userMessage]);
    if (!customText) setInput('');
    setLoading(true);

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: textToSend,
          agentId: selectedAgentId,
          model: selectedModel,
          messages: [...messages, userMessage].map((m) => ({
            role: m.role,
            content: m.content,
          })),
        }),
      });

      if (res.ok) {
        const data = await res.json();
        const assistantMsg: ChatMessage = {
          id: `msg-a-${Date.now()}`,
          role: 'assistant',
          agentId: data.agentId || selectedAgent.id,
          agentName: data.agentName || selectedAgent.name,
          agentAvatar: data.agentAvatar || selectedAgent.avatar,
          content: data.reply,
          timestamp: new Date().toISOString(),
          model: data.model || selectedModel,
          thinkingProcess: data.thinkingProcess,
          toolInvocations: data.toolInvocations,
          suggestedWorkflow: data.suggestedWorkflow,
          tokensUsed: data.tokensUsed,
          costUsd: data.costUsd,
        };

        setMessages((prev) => [...prev, assistantMsg]);
        if (data.thinkingProcess) {
          setExpandedThinking((prev) => ({ ...prev, [assistantMsg.id]: true }));
        }
      } else {
        throw new Error('API returned error');
      }
    } catch (err) {
      console.error('Chat execution failed', err);
      const errorMsg: ChatMessage = {
        id: `msg-err-${Date.now()}`,
        role: 'assistant',
        agentId: selectedAgent.id,
        agentName: selectedAgent.name,
        agentAvatar: selectedAgent.avatar,
        content: `⚠️ Failed to reach agent ${selectedAgent.name}. Please ensure the Beagle dev server is active.`,
        timestamp: new Date().toISOString(),
        model: selectedModel,
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  const handleCopyCode = (code: string, id: string) => {
    navigator.clipboard.writeText(code);
    setCopiedCodeId(id);
    setTimeout(() => setCopiedCodeId(null), 2000);
  };

  const handleClearChat = () => {
    setMessages([initialMessage]);
  };

  const handleExportChat = () => {
    const transcript = messages
      .map(
        (m) =>
          `### ${m.role === 'user' ? 'User' : `${m.agentName} (${m.model})`} [${new Date(
            m.timestamp
          ).toLocaleTimeString()}]\n\n${m.content}\n\n---\n`
      )
      .join('\n');
    const blob = new Blob([transcript], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `beagle-agent-transcript-${new Date().toISOString().slice(0, 10)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const renderInlineStyles = (text: string) => {
    const segments = text.split(/(\*\*.*?\*\*|`.*?`)/g);
    return segments.map((seg, i) => {
      if (seg.startsWith('**') && seg.endsWith('**')) {
        return (
          <strong key={i} className="text-zinc-100 font-semibold">
            {seg.slice(2, -2)}
          </strong>
        );
      }
      if (seg.startsWith('`') && seg.endsWith('`')) {
        return (
          <code
            key={i}
            className="px-1.5 py-0.5 rounded-md bg-white/[0.06] border border-white/[0.08] text-orange-300 font-mono text-[11px]"
          >
            {seg.slice(1, -1)}
          </code>
        );
      }
      return seg;
    });
  };

  const renderFormattedContent = (content: string, messageId: string) => {
    const parts = content.split(/(```[\s\S]*?```)/g);

    return parts.map((part, index) => {
      if (part.startsWith('```')) {
        const lines = part.slice(3, -3).trim().split('\n');
        const lang = lines[0]?.trim() || 'code';
        const code = lines.slice(1).join('\n');
        const codeId = `${messageId}-${index}`;

        return (
          <div
            key={index}
            className="my-3 rounded-xl overflow-hidden border border-white/[0.08] bg-[#09090d] font-mono text-xs shadow-md"
          >
            <div className="bg-white/[0.03] px-3.5 py-2 border-b border-white/[0.06] flex items-center justify-between text-zinc-400">
              <div className="flex items-center gap-2">
                <Code2 className="w-3.5 h-3.5 text-orange-400" />
                <span className="text-[10px] font-semibold text-orange-300 uppercase tracking-wider font-mono">
                  {lang}
                </span>
              </div>
              <button
                onClick={() => handleCopyCode(code, codeId)}
                className="flex items-center gap-1.5 text-[10px] text-zinc-400 hover:text-white transition-colors px-2 py-1 rounded-md bg-white/[0.04] hover:bg-white/[0.08] border border-white/[0.06] cursor-pointer"
              >
                {copiedCodeId === codeId ? (
                  <>
                    <Check className="w-3 h-3 text-emerald-400" />
                    <span className="text-emerald-400 font-medium">Copied</span>
                  </>
                ) : (
                  <>
                    <Copy className="w-3 h-3" />
                    <span>Copy snippet</span>
                  </>
                )}
              </button>
            </div>
            <div className="p-4 overflow-x-auto text-zinc-200 leading-relaxed font-mono text-[11px]">
              <pre>
                <code>{code}</code>
              </pre>
            </div>
          </div>
        );
      }

      const formattedLines = part.split('\n').map((line, lineIdx) => {
        if (line.startsWith('### ')) {
          return (
            <h4 key={lineIdx} className="text-xs font-bold text-white uppercase tracking-wider mt-3 mb-1.5 font-mono flex items-center gap-1.5 text-orange-300">
              <span className="w-1 h-3 rounded-full bg-orange-500"></span>
              {line.replace('### ', '')}
            </h4>
          );
        }
        if (line.startsWith('#### ')) {
          return (
            <h5 key={lineIdx} className="text-xs font-semibold text-zinc-200 mt-2.5 mb-1 font-mono">
              {line.replace('#### ', '')}
            </h5>
          );
        }
        if (line.startsWith('- ') || line.startsWith('* ')) {
          return (
            <div key={lineIdx} className="flex items-start gap-2 text-zinc-300 my-1 text-[13px] leading-relaxed">
              <span className="w-1.5 h-1.5 rounded-full bg-orange-400/80 mt-2 shrink-0"></span>
              <span className="flex-1">{renderInlineStyles(line.slice(2))}</span>
            </div>
          );
        }
        if (/^\d+\.\s/.test(line)) {
          const num = line.match(/^(\d+)\.\s/)?.[1] || '1';
          return (
            <div key={lineIdx} className="flex items-start gap-2 text-zinc-300 my-1 text-[13px] leading-relaxed">
              <span className="font-mono text-[11px] text-orange-400 shrink-0 mt-0.5">{num}.</span>
              <span className="flex-1">{renderInlineStyles(line.replace(/^\d+\.\s/, ''))}</span>
            </div>
          );
        }
        if (!line.trim()) {
          return <div key={lineIdx} className="h-1.5" />;
        }
        return (
          <p key={lineIdx} className="my-1 text-[13px] leading-relaxed text-zinc-300">
            {renderInlineStyles(line)}
          </p>
        );
      });

      return <div key={index}>{formattedLines}</div>;
    });
  };

  const totalConversationSpend = messages.reduce((sum, m) => sum + (m.costUsd || 0), 0);
  const totalTokens = messages.reduce((sum, m) => sum + (m.tokensUsed || 0), 0);

  return (
    <div className="flex flex-col h-[calc(100vh-8rem)] bg-white/[0.02] border border-white/[0.06] rounded-3xl overflow-hidden shadow-2xl backdrop-blur-xl relative">
      {/* Top Bar: Agent & Model Selector & Controls */}
      <div className="bg-[#0a0a0f]/80 backdrop-blur-xl border-b border-white/[0.06] px-4 py-3 flex flex-wrap items-center justify-between gap-3 shrink-0">
        {/* Agent Selector */}
        <div className="flex items-center gap-2.5">
          <div className="flex items-center gap-2 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1.5 rounded-xl">
            <span className="text-base">{selectedAgent.avatar}</span>
            <div className="flex flex-col">
              <span className="text-xs font-semibold text-white tracking-tight">{selectedAgent.name}</span>
              <span className="text-[9px] font-mono text-orange-400">{selectedAgent.role}</span>
            </div>
            <select
              id="select-chat-agent"
              value={selectedAgentId}
              onChange={(e) => setSelectedAgentId(e.target.value)}
              className="ml-1 bg-transparent border-0 text-zinc-300 text-xs font-mono focus:outline-none cursor-pointer"
            >
              {agents.map((ag) => (
                <option key={ag.id} value={ag.id} className="bg-zinc-900 text-white">
                  {ag.avatar} {ag.name}
                </option>
              ))}
            </select>
          </div>

          {/* Model Selector Pill */}
          <div className="hidden sm:flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1.5 rounded-xl text-xs font-mono text-zinc-300">
            <Flame className="w-3.5 h-3.5 text-orange-400" />
            <select
              id="select-chat-model"
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              className="bg-transparent text-zinc-200 focus:outline-none cursor-pointer text-[11px]"
            >
              <option value="gemini-3.7-flash" className="bg-zinc-900">Gemini 3.7 Flash</option>
              <option value="claude-3.7-sonnet" className="bg-zinc-900">Claude 3.7 Sonnet</option>
              <option value="gemini-3.1-pro-preview" className="bg-zinc-900">Gemini 3.1 Pro</option>
              <option value="beagle-microvm-local" className="bg-zinc-900">Beagle Local MicroVM</option>
            </select>
          </div>
        </div>

        {/* Live Token & Cost Governance Meter */}
        <div className="flex items-center gap-2 text-xs font-mono">
          <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1 rounded-xl">
            <CircleDollarSign className="w-3.5 h-3.5 text-orange-400" />
            <span className="text-zinc-400 text-[10px]">Session:</span>
            <span className="text-orange-400 font-semibold text-[11px]">${totalConversationSpend.toFixed(4)}</span>
          </div>

          <div className="hidden md:flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] px-2.5 py-1 rounded-xl">
            <Zap className="w-3.5 h-3.5 text-amber-400" />
            <span className="text-zinc-400 text-[10px]">Tokens:</span>
            <span className="text-zinc-200 text-[11px]">{totalTokens.toLocaleString()}</span>
          </div>

          <button
            id="btn-export-chat"
            onClick={handleExportChat}
            title="Export Transcript (.md)"
            className="p-1.5 text-zinc-400 hover:text-white hover:bg-white/[0.06] rounded-xl transition-colors cursor-pointer border border-transparent hover:border-white/[0.06]"
          >
            <Download className="w-3.5 h-3.5" />
          </button>

          <button
            id="btn-clear-chat"
            onClick={handleClearChat}
            title="Clear Chat"
            className="p-1.5 text-zinc-400 hover:text-rose-400 hover:bg-white/[0.06] rounded-xl transition-colors cursor-pointer border border-transparent hover:border-white/[0.06]"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Chat Messages List */}
      <div className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-6">
        {messages.map((message) => {
          const isUser = message.role === 'user';
          const isThinkingExpanded = expandedThinking[message.id];

          return (
            <div
              key={message.id}
              className={`flex gap-3 max-w-4xl ${isUser ? 'ml-auto justify-end' : 'mr-auto justify-start'}`}
            >
              {/* Agent Avatar */}
              {!isUser && (
                <div className="w-8 h-8 rounded-xl bg-gradient-to-tr from-orange-600 via-amber-600 to-orange-500 flex items-center justify-center text-sm shadow-md shadow-orange-500/20 shrink-0 border border-orange-400/30">
                  {message.agentAvatar}
                </div>
              )}

              {/* Message Bubble Container */}
              <div className={`space-y-2 max-w-[88%] sm:max-w-[82%] ${isUser ? 'items-end' : 'items-start'}`}>
                {/* Header info */}
                <div className="flex items-center gap-2 text-[10px] font-mono text-zinc-400 px-1">
                  <span className="font-semibold text-zinc-300">
                    {isUser ? 'You (Operator)' : message.agentName}
                  </span>
                  {!isUser && (
                    <span className="px-1.5 py-0.2 rounded-md bg-white/[0.04] border border-white/[0.06] text-orange-400 text-[9px]">
                      {message.model}
                    </span>
                  )}
                  <span className="text-zinc-500">{new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                </div>

                {/* Thinking Process Accordion (Claude/Reasoning style) */}
                {message.thinkingProcess && (
                  <div className="border border-white/[0.06] rounded-xl overflow-hidden text-xs bg-white/[0.02]">
                    <button
                      onClick={() =>
                        setExpandedThinking((prev) => ({
                          ...prev,
                          [message.id]: !prev[message.id],
                        }))
                      }
                      className="w-full px-3 py-1.5 bg-black/40 hover:bg-white/[0.04] flex items-center justify-between text-zinc-400 font-mono text-[10px] transition-colors cursor-pointer"
                    >
                      <span className="flex items-center gap-1.5 text-orange-400/90 font-medium">
                        <Sparkles className="w-3 h-3 animate-pulse" />
                        Reasoning Trace & Spec Invariants
                      </span>
                      {isThinkingExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                    </button>
                    {isThinkingExpanded && (
                      <div className="p-3 bg-black/60 font-mono text-[11px] text-zinc-400 whitespace-pre-line border-t border-white/[0.04] leading-relaxed">
                        {message.thinkingProcess}
                      </div>
                    )}
                  </div>
                )}

                {/* Message Content Bubble */}
                <div
                  className={`rounded-2xl p-4 text-xs sm:text-sm leading-relaxed ${
                    isUser
                      ? 'bg-gradient-to-r from-orange-600 via-amber-600 to-orange-500 text-white font-normal shadow-lg shadow-orange-500/15 border border-orange-400/25'
                      : 'bg-white/[0.03] border border-white/[0.06] text-zinc-200 shadow-sm backdrop-blur-md'
                  }`}
                >
                  {isUser ? message.content : renderFormattedContent(message.content, message.id)}
                </div>

                {/* Suggested DAG Workflow Card */}
                {message.suggestedWorkflow && (
                  <div className="bg-gradient-to-br from-orange-950/30 to-black border border-orange-500/40 rounded-2xl p-4 space-y-3 shadow-lg shadow-orange-500/5">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <Layers className="w-4 h-4 text-orange-400" />
                        <h5 className="text-xs font-bold text-zinc-100">
                          {message.suggestedWorkflow.name}
                        </h5>
                      </div>
                      <span className="text-[10px] font-mono text-orange-300 bg-white/[0.04] px-2 py-0.5 rounded-md border border-orange-500/20">
                        {message.suggestedWorkflow.nodesCount} Nodes • Est. ${message.suggestedWorkflow.estimatedCost.toFixed(2)}
                      </span>
                    </div>

                    <p className="text-[11px] text-zinc-400 leading-normal">
                      Goal: {message.suggestedWorkflow.goal}
                    </p>

                    <button
                      id={`btn-launch-dag-${message.id}`}
                      onClick={() =>
                        onExecuteWorkflow(
                          message.suggestedWorkflow!.id,
                          message.suggestedWorkflow!.goal,
                          message.suggestedWorkflow!.estimatedCost
                        )
                      }
                      className="w-full py-2 bg-gradient-to-r from-orange-600 to-amber-600 hover:from-orange-500 hover:to-amber-500 text-white rounded-xl text-xs font-semibold flex items-center justify-center gap-1.5 shadow-md shadow-orange-500/20 transition-all cursor-pointer"
                    >
                      <Play className="w-3.5 h-3.5 fill-white" />
                      <span>Launch Pipeline in Execution Engine</span>
                      <ArrowUpRight className="w-3.5 h-3.5" />
                    </button>
                  </div>
                )}
              </div>

              {/* User Avatar */}
              {isUser && (
                <div className="w-8 h-8 rounded-xl bg-white/[0.06] border border-white/[0.1] flex items-center justify-center text-sm shadow-md shrink-0">
                  👤
                </div>
              )}
            </div>
          );
        })}

        {/* Loading Indicator */}
        {loading && (
          <div className="flex gap-3 max-w-2xl mr-auto">
            <div className="w-8 h-8 rounded-xl bg-gradient-to-tr from-orange-600 to-amber-600 flex items-center justify-center text-sm shadow-md shadow-orange-500/20 shrink-0 animate-pulse">
              {selectedAgent.avatar}
            </div>
            <div className="bg-white/[0.03] border border-white/[0.06] rounded-2xl p-4 space-y-2 text-xs backdrop-blur-md">
              <div className="flex items-center gap-2 text-orange-400 font-mono text-[11px]">
                <Sparkles className="w-3.5 h-3.5 animate-spin" />
                <span>{selectedAgent.name} is reasoning & executing AST query...</span>
              </div>
              <div className="flex gap-1.5 pt-0.5">
                <span className="w-1.5 h-1.5 rounded-full bg-orange-500 animate-bounce"></span>
                <span className="w-1.5 h-1.5 rounded-full bg-orange-400 animate-bounce [animation-delay:0.15s]"></span>
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-bounce [animation-delay:0.3s]"></span>
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Starter Directives / Prompt Chips */}
      {messages.length <= 3 && (
        <div className="px-4 py-2.5 border-t border-white/[0.06] bg-black/40">
          <span className="text-[10px] font-mono text-zinc-400 uppercase tracking-wider block mb-2 font-medium">
            Suggested Directives:
          </span>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
            {PROMPT_PRESETS.map((preset, idx) => (
              <button
                key={idx}
                id={`btn-preset-${idx}`}
                onClick={() => {
                  setSelectedAgentId(preset.agentId);
                  handleSendMessage(preset.prompt);
                }}
                className="text-left p-2.5 rounded-xl bg-white/[0.02] hover:bg-white/[0.05] border border-white/[0.05] hover:border-orange-500/40 transition-all text-xs group cursor-pointer"
              >
                <div className="font-medium text-zinc-200 group-hover:text-orange-400 flex items-center justify-between text-[11px]">
                  <span>{preset.title}</span>
                  <ArrowUpRight className="w-3 h-3 text-zinc-400 group-hover:text-orange-400 transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
                </div>
                <div className="text-[10px] text-zinc-400 mt-0.5 line-clamp-1">{preset.desc}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Input Area (Claude / ChatGPT style) */}
      <div className="p-3 sm:p-4 bg-[#09090d]/90 backdrop-blur-xl border-t border-white/[0.06]">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSendMessage();
          }}
          className="relative bg-white/[0.03] border border-white/[0.08] focus-within:border-orange-500/60 focus-within:ring-1 focus-within:ring-orange-500/30 rounded-2xl p-2.5 transition-all shadow-inner"
        >
          <textarea
            ref={textareaRef}
            rows={2}
            id="input-chat-prompt"
            placeholder={`Message ${selectedAgent.name} (e.g. 'Audit security boundary', 'Plan 4-node DAG', 'Query AST call graph')...`}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            className="w-full bg-transparent text-xs sm:text-sm text-zinc-100 placeholder-zinc-400 focus:outline-none resize-none px-2 py-1 font-sans"
          />

          <div className="flex items-center justify-between pt-2 border-t border-white/[0.04] px-2 text-[10px] text-zinc-400 font-mono">
            <div className="flex items-center gap-2">
              <span className="hidden sm:inline">Enter to send, Shift+Enter for newline</span>
              <span className="sm:hidden">Zero-Trust Agent</span>
            </div>

            <button
              type="submit"
              id="btn-chat-send"
              disabled={!input.trim() || loading}
              className={`p-2 rounded-xl text-white font-medium flex items-center justify-center transition-all ${
                input.trim() && !loading
                  ? 'bg-gradient-to-r from-orange-600 to-amber-600 hover:from-orange-500 hover:to-amber-500 shadow-md shadow-orange-500/25 cursor-pointer'
                  : 'bg-white/[0.04] text-zinc-400 cursor-not-allowed'
              }`}
            >
              <Send className="w-3.5 h-3.5" />
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
