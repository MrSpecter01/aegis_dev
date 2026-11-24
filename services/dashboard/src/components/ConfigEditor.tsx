import React, { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import {
  getSystemConfig,
  updateSystemConfig,
  getAgentsConfig,
  updateAgentConfig,
  getAgentsHistory,
  rollbackAgentsConfig,
  getSagConfig,
  updateSagToolPolicy,
  getSagHistory,
  rollbackSagConfig,
  getMcpConfig,
  updateMcpConfig,
  getProviderConfig,
  updateProviderConfig,
  getVersionDetails,
  getIdentitiesConfig,
  getDriftStatus,
  checkDrift,
  approveDrift,
  rejectDrift,
  syncDriftTarget,
} from '../api/client';
import {
  Settings,
  Shield,
  Layers,
  Activity,
  RefreshCw,
  CheckCircle,
  AlertTriangle,
  Key,
} from './Icons';
import {
  SagPoliciesConfig,
  AgentsConfig,
  AgentRoleConfig,
  ModelConfig,
  ConfigVersionItem,
  MCPServerConfig,
  VersionDetails,
  IdentitiesPublicResponse,
  DriftStatusResponse,
} from '../types';

export const ConfigEditor: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'agents' | 'sag' | 'mcp' | 'system' | 'providers'>('agents');
  const [agentsConfig, setAgentsConfig] = useState<AgentsConfig | null>(null);
  const [sagConfig, setSagConfig] = useState<SagPoliciesConfig | null>(null);
  const [mcpConfig, setMcpConfig] = useState<Record<string, any> | null>(null);
  const [systemConfig, setSystemConfig] = useState<Record<string, any> | null>(null);
  const [providerConfig, setProviderConfig] = useState<any>(null);
  const [identitiesConfig, setIdentitiesConfig] = useState<IdentitiesPublicResponse | null>(null);
  const [driftStatus, setDriftStatus] = useState<DriftStatusResponse | null>(null);
  const [driftBusy, setDriftBusy] = useState<string | null>(null);
  const [driftFeedback, setDriftFeedback] = useState<{ text: string; isError: boolean } | null>(null);
  const [googleKeyInput, setGoogleKeyInput] = useState('');
  const [openaiKeyInput, setOpenaiKeyInput] = useState('');
  const [anthropicKeyInput, setAnthropicKeyInput] = useState('');
  const [ollamaUrlInput, setOllamaUrlInput] = useState('');
  const [savingProviders, setSavingProviders] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Agent editing state
  const [editingAgent, setEditingAgent] = useState<string | null>(null);
  const [agentDraft, setAgentDraft] = useState<AgentRoleConfig | null>(null);

  // Version history state
  const [historyModalOpen, setHistoryModalOpen] = useState(false);
  const [historyList, setHistoryList] = useState<ConfigVersionItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyTarget, setHistoryTarget] = useState<'agents' | 'sag'>('agents');
  const [expandedVersions, setExpandedVersions] = useState<Record<string, boolean>>({});
  const [versionDetailsMap, setVersionDetailsMap] = useState<Record<string, VersionDetails>>({});
  const [loadingVersionId, setLoadingVersionId] = useState<string | null>(null);
  const [confirmRollbackModal, setConfirmRollbackModal] = useState<{
    versionId: string;
    details: VersionDetails | null;
  } | null>(null);
  const [rollingBack, setRollingBack] = useState(false);

  // Raw editor state (for MCP & System)
  const [rawEditorOpen, setRawEditorOpen] = useState(false);
  const [rawEditorTarget, setRawEditorTarget] = useState<'mcp' | 'system'>('mcp');
  const [rawContent, setRawContent] = useState('');

  // SAG Filter state
  const [sagCategoryFilter, setSagCategoryFilter] = useState('ALL');
  const [sagApprovalFilter, setSagApprovalFilter] = useState('ALL');

  // Confirmation Modal States ("old -> new")
  const [confirmAgentModal, setConfirmAgentModal] = useState<{
    role: string;
    oldConfig: AgentRoleConfig;
    newConfig: AgentRoleConfig;
    diffs: Array<{ field: string; oldVal: string; newVal: string }>;
  } | null>(null);

  const [confirmSagModal, setConfirmSagModal] = useState<{
    toolName: string;
    field: 'pre_approved' | 'requires_approval';
    oldVal: boolean;
    newVal: boolean;
  } | null>(null);

  const [confirmProviderModal, setConfirmProviderModal] = useState<{
    diffs: Array<{ field: string; oldVal: string; newVal: string }>;
    payload: any;
  } | null>(null);

  const [confirmRawModal, setConfirmRawModal] = useState<{
    target: 'mcp' | 'system';
    parsed: any;
    diffSummary: Array<{ key: string; status: 'modified' | 'added' | 'removed' }>;
  } | null>(null);

  const [savingAction, setSavingAction] = useState(false);

  const showNotification = (msg: string, isError = false) => {
    if (isError) {
      setErrorMessage(msg);
      setTimeout(() => setErrorMessage(null), 5000);
    } else {
      setSaveMessage(msg);
      setTimeout(() => setSaveMessage(null), 4000);
    }
  };

  const setDriftNotice = (text: string, isError = false) => {
    setDriftFeedback({ text, isError });
    showNotification(text, isError);
  };

  const loadAll = async () => {
    setLoading(true);
    try {
      const [agents, sag, mcp, sys, prov, idents, drift] = await Promise.all([
        getAgentsConfig().catch(() => null),
        getSagConfig().catch(() => null),
        getMcpConfig().catch(() => null),
        getSystemConfig().catch(() => null),
        getProviderConfig().catch(() => null),
        getIdentitiesConfig().catch(() => null),
        getDriftStatus().catch(() => null),
      ]);
      setAgentsConfig(agents);
      setSagConfig(sag);
      setMcpConfig(mcp);
      setSystemConfig(sys);
      setProviderConfig(prov);
      setIdentitiesConfig(idents);
      setDriftStatus(drift);
      if (prov) {
        setOllamaUrlInput(prov.ollama_url || '');
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAll();
  }, []);

  // =========================================================================
  // Agent Edit Handlers & Old -> New Prompt
  // =========================================================================

  const startEditAgent = (role: string, config: AgentRoleConfig) => {
    setEditingAgent(role);
    setAgentDraft(JSON.parse(JSON.stringify(config)));
  };

  const handlePromptSaveAgentDraft = () => {
    if (!editingAgent || !agentDraft) return;
    const oldConfig = agentsConfig?.agents?.[editingAgent];
    if (!oldConfig) {
      executeSaveAgentDraft();
      return;
    }

    const diffs: Array<{ field: string; oldVal: string; newVal: string }> = [];

    if (oldConfig.primary?.provider !== agentDraft.primary?.provider) {
      diffs.push({
        field: 'Primary Provider',
        oldVal: oldConfig.primary?.provider || '—',
        newVal: agentDraft.primary?.provider || '—',
      });
    }
    if (oldConfig.primary?.model !== agentDraft.primary?.model) {
      diffs.push({
        field: 'Primary Model',
        oldVal: oldConfig.primary?.model || '—',
        newVal: agentDraft.primary?.model || '—',
      });
    }
    if (oldConfig.primary?.temperature !== agentDraft.primary?.temperature) {
      diffs.push({
        field: 'Temperature',
        oldVal: String(oldConfig.primary?.temperature ?? 0.2),
        newVal: String(agentDraft.primary?.temperature ?? 0.2),
      });
    }
    if (oldConfig.primary?.token_limit !== agentDraft.primary?.token_limit) {
      diffs.push({
        field: 'Token Limit',
        oldVal: String(oldConfig.primary?.token_limit ?? 8000),
        newVal: String(agentDraft.primary?.token_limit ?? 8000),
      });
    }
    if ((oldConfig.timeout_ms || 30000) !== (agentDraft.timeout_ms || 30000)) {
      diffs.push({
        field: 'Timeout',
        oldVal: `${oldConfig.timeout_ms || 30000}ms`,
        newVal: `${agentDraft.timeout_ms || 30000}ms`,
      });
    }

    // Compare Fallback Models
    const oldFallbacks = (oldConfig.fallback || [])
      .map((f, i) => `#${i + 1}: ${f.provider}/${f.model}`)
      .join(' → ') || 'None';
    const newFallbacks = (agentDraft.fallback || [])
      .map((f, i) => `#${i + 1}: ${f.provider}/${f.model}`)
      .join(' → ') || 'None';
    if (oldFallbacks !== newFallbacks) {
      diffs.push({
        field: 'Fallback Chain',
        oldVal: oldFallbacks,
        newVal: newFallbacks,
      });
    }

    setConfirmAgentModal({
      role: editingAgent,
      oldConfig,
      newConfig: agentDraft,
      diffs,
    });
  };

  const executeSaveAgentDraft = async () => {
    if (!editingAgent || !agentDraft) return;
    setSavingAction(true);
    try {
      const res = await updateAgentConfig(editingAgent, agentDraft);
      if (res.all_agents) {
        setAgentsConfig(res.all_agents);
      } else {
        const refreshed = await getAgentsConfig();
        setAgentsConfig(refreshed);
      }
      showNotification(`Saved and persisted configuration for agent '${editingAgent}'`);
      setConfirmAgentModal(null);
      setEditingAgent(null);
      setAgentDraft(null);
    } catch (err: any) {
      showNotification(`Failed to save agent: ${err.message}`, true);
    } finally {
      setSavingAction(false);
    }
  };

  const handleAddFallbackModel = () => {
    if (!agentDraft) return;
    const currentFallbacks = agentDraft.fallback || [];
    const newFallback: ModelConfig = {
      provider: 'gemini',
      model: 'gemma-4-31b-it',
      temperature: 0.2,
      token_limit: 8000,
    };
    setAgentDraft({
      ...agentDraft,
      fallback: [...currentFallbacks, newFallback],
    });
  };

  const handleRemoveFallbackModel = (idx: number) => {
    if (!agentDraft || !agentDraft.fallback) return;
    const next = [...agentDraft.fallback];
    next.splice(idx, 1);
    setAgentDraft({ ...agentDraft, fallback: next });
  };

  const handleUpdateFallback = (idx: number, patch: Partial<ModelConfig>) => {
    if (!agentDraft || !agentDraft.fallback) return;
    const next = [...agentDraft.fallback];
    next[idx] = { ...next[idx], ...patch };
    setAgentDraft({ ...agentDraft, fallback: next });
  };

  // =========================================================================
  // History & Rollback Handlers (with Diff Dropdown and Confirmation Modal)
  // =========================================================================

  const openHistory = async (target: 'agents' | 'sag') => {
    setHistoryTarget(target);
    setHistoryModalOpen(true);
    setHistoryLoading(true);
    setExpandedVersions({});
    try {
      const items = target === 'agents' ? await getAgentsHistory() : await getSagHistory();
      setHistoryList(items);
    } catch (err: any) {
      showNotification(`Failed to fetch history: ${err.message}`, true);
    } finally {
      setHistoryLoading(false);
    }
  };

  const handleToggleVersionDetails = async (versionId: string) => {
    const isExpanded = !!expandedVersions[versionId];
    if (isExpanded) {
      setExpandedVersions((prev) => ({ ...prev, [versionId]: false }));
      return;
    }
    setExpandedVersions((prev) => ({ ...prev, [versionId]: true }));
    if (!versionDetailsMap[versionId]) {
      setLoadingVersionId(versionId);
      try {
        const details = await getVersionDetails(versionId);
        setVersionDetailsMap((prev) => ({ ...prev, [versionId]: details }));
      } catch (err: any) {
        showNotification(`Failed to load version diff: ${err.message}`, true);
      } finally {
        setLoadingVersionId(null);
      }
    }
  };

  const handleInitiateRollback = async (versionId: string) => {
    let details = versionDetailsMap[versionId] || null;
    if (!details) {
      try {
        details = await getVersionDetails(versionId);
        setVersionDetailsMap((prev) => ({ ...prev, [versionId]: details }));
      } catch (err: any) {
        console.error('Failed to pre-fetch version details:', err);
      }
    }
    setConfirmRollbackModal({ versionId, details });
  };

  const executeRollback = async () => {
    if (!confirmRollbackModal) return;
    setRollingBack(true);
    const { versionId } = confirmRollbackModal;
    try {
      if (historyTarget === 'agents') {
        const res = await rollbackAgentsConfig(versionId);
        setAgentsConfig(res.agents);
        showNotification(`Successfully restored agents configuration to version ${versionId}`);
      } else {
        const res = await rollbackSagConfig(versionId);
        setSagConfig(res.sag);
        showNotification(`Successfully restored SAG policies to version ${versionId}`);
      }
      setConfirmRollbackModal(null);
      setHistoryModalOpen(false);
    } catch (err: any) {
      showNotification(`Rollback failed: ${err.message}`, true);
    } finally {
      setRollingBack(false);
    }
  };

  // Diff lines renderer
  const renderDiffLines = (diffText: string) => {
    if (!diffText || !diffText.trim()) {
      return (
        <div style={{ padding: '14px', color: '#94a3b8', fontSize: '0.775rem', fontStyle: 'italic', textAlign: 'center' }}>
          No differences between this version snapshot and current active configuration.
        </div>
      );
    }

    const lines = diffText.split('\n');
    return (
      <div style={{
        backgroundColor: '#07090e',
        border: '1px solid var(--border-medium)',
        borderRadius: '6px',
        padding: '10px 12px',
        maxHeight: '300px',
        overflowY: 'auto',
        fontFamily: 'var(--font-mono)',
        fontSize: '0.75rem',
        lineHeight: 1.5,
      }}>
        {lines.map((line, idx) => {
          let color = '#cbd5e1';
          let bg = 'transparent';
          let fw = 400;

          if (line.startsWith('+++') || line.startsWith('---')) {
            color = '#94a3b8';
            fw = 600;
          } else if (line.startsWith('+')) {
            color = '#86efac';
            bg = 'rgba(34, 197, 94, 0.12)';
          } else if (line.startsWith('-')) {
            color = '#fca5a5';
            bg = 'rgba(239, 68, 68, 0.12)';
          } else if (line.startsWith('@@')) {
            color = '#c084fc';
            fw = 600;
          }

          return (
            <div
              key={idx}
              style={{
                color,
                backgroundColor: bg,
                fontWeight: fw,
                padding: '1px 4px',
                borderRadius: '2px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-all',
              }}
            >
              {line || ' '}
            </div>
          );
        })}
      </div>
    );
  };

  // =========================================================================
  // SAG Policy Handlers & Old -> New Prompt
  // =========================================================================

  const handlePromptTogglePolicy = (
    toolName: string,
    field: 'pre_approved' | 'requires_approval',
    currentVal: boolean
  ) => {
    setConfirmSagModal({
      toolName,
      field,
      oldVal: currentVal,
      newVal: !currentVal,
    });
  };

  const executeToggleSagPolicy = async () => {
    if (!confirmSagModal) return;
    setSavingAction(true);
    const { toolName, field, newVal } = confirmSagModal;
    try {
      const res = await updateSagToolPolicy(toolName, { [field]: newVal });
      if (res.sag) {
        setSagConfig(res.sag);
      } else {
        const updated = await getSagConfig();
        setSagConfig(updated);
      }
      showNotification(`Policy for tool '${toolName}' updated: ${field} = ${newVal}`);
      setConfirmSagModal(null);
    } catch (err: any) {
      showNotification(`Failed to update tool policy: ${err.message}`, true);
    } finally {
      setSavingAction(false);
    }
  };

  // =========================================================================
  // LLM Providers Handlers & Old -> New Prompt
  // =========================================================================

  const handlePromptSaveProviders = () => {
    const diffs: Array<{ field: string; oldVal: string; newVal: string }> = [];
    const payload: any = {};

    if (googleKeyInput.trim()) {
      payload.google_api_key = googleKeyInput.trim();
      diffs.push({
        field: 'Google Gemini API Key',
        oldVal: providerConfig?.google_api_key_set ? 'Configured (Masked)' : 'Not configured',
        newVal: `Updated (${googleKeyInput.trim().slice(0, 4)}...${googleKeyInput.trim().slice(-4)})`,
      });
    }

    if (openaiKeyInput.trim()) {
      payload.openai_api_key = openaiKeyInput.trim();
      diffs.push({
        field: 'OpenAI API Key',
        oldVal: providerConfig?.openai_api_key_set ? 'Configured (Masked)' : 'Not configured',
        newVal: `Updated (${openaiKeyInput.trim().slice(0, 3)}...${openaiKeyInput.trim().slice(-4)})`,
      });
    }

    if (anthropicKeyInput.trim()) {
      payload.anthropic_api_key = anthropicKeyInput.trim();
      diffs.push({
        field: 'Anthropic API Key',
        oldVal: providerConfig?.anthropic_api_key_set ? 'Configured (Masked)' : 'Not configured',
        newVal: `Updated (${anthropicKeyInput.trim().slice(0, 7)}...${anthropicKeyInput.trim().slice(-4)})`,
      });
    }

    const currentOllama = providerConfig?.ollama_url || 'http://localhost:11434';
    const newOllama = ollamaUrlInput.trim() || 'http://localhost:11434';
    if (newOllama !== currentOllama) {
      payload.ollama_url = newOllama;
      diffs.push({
        field: 'Ollama Base URL',
        oldVal: currentOllama,
        newVal: newOllama,
      });
    }

    if (diffs.length === 0) {
      showNotification('No provider credentials or endpoint URLs were modified.', true);
      return;
    }

    setConfirmProviderModal({ diffs, payload });
  };

  const executeSaveProviders = async () => {
    if (!confirmProviderModal) return;
    setSavingProviders(true);
    try {
      await updateProviderConfig(confirmProviderModal.payload);
      const refreshed = await getProviderConfig();
      setProviderConfig(refreshed);
      setGoogleKeyInput('');
      setOpenaiKeyInput('');
      setAnthropicKeyInput('');
      showNotification('Successfully saved and persisted LLM provider keys & endpoints.');
      setConfirmProviderModal(null);
    } catch (err: any) {
      showNotification(`Failed to save provider keys: ${err.message}`, true);
    } finally {
      setSavingProviders(false);
    }
  };

  // =========================================================================
  // Raw JSON Editor Handlers & Old -> New Prompt
  // =========================================================================

  const openRawEditor = (target: 'mcp' | 'system') => {
    setRawEditorTarget(target);
    const content = target === 'mcp' ? mcpConfig : systemConfig;
    setRawContent(JSON.stringify(content || {}, null, 2));
    setRawEditorOpen(true);
  };

  const handlePromptSaveRawContent = () => {
    try {
      const parsed = JSON.parse(rawContent);
      const current = (rawEditorTarget === 'mcp' ? mcpConfig : systemConfig) || {};

      const diffSummary: Array<{ key: string; status: 'modified' | 'added' | 'removed' }> = [];
      const allKeys = Array.from(new Set([...Object.keys(current), ...Object.keys(parsed || {})]));

      for (const k of allKeys) {
        if (!(k in current)) {
          diffSummary.push({ key: k, status: 'added' });
        } else if (!(k in parsed)) {
          diffSummary.push({ key: k, status: 'removed' });
        } else if (JSON.stringify(current[k]) !== JSON.stringify(parsed[k])) {
          diffSummary.push({ key: k, status: 'modified' });
        }
      }

      setConfirmRawModal({
        target: rawEditorTarget,
        parsed,
        diffSummary,
      });
    } catch (err: any) {
      showNotification(`Invalid JSON: ${err.message}`, true);
    }
  };

  const executeSaveRawContent = async () => {
    if (!confirmRawModal) return;
    setSavingAction(true);
    try {
      const { target, parsed } = confirmRawModal;
      if (target === 'mcp') {
        await updateMcpConfig(parsed);
        setMcpConfig(parsed);
        showNotification('MCP Servers configuration updated successfully.');
      } else {
        await updateSystemConfig(parsed);
        setSystemConfig(parsed);
        showNotification('System configuration updated successfully.');
      }
      setConfirmRawModal(null);
      setRawEditorOpen(false);
    } catch (err: any) {
      showNotification(`Failed to save configuration: ${err.message}`, true);
    } finally {
      setSavingAction(false);
    }
  };

  // =========================================================================
  // Tool Categorization
  // =========================================================================

  const categorizeTools = () => {
    const tools = sagConfig?.policies?.tools || sagConfig?.tools || {};
    const categories: Record<string, { label: string; tools: Array<{ name: string; policy: any }> }> = {
      k8s_write: { label: 'Kubernetes Mutating Tools (Write)', tools: [] },
      k8s_read: { label: 'Kubernetes Status & Inspection Tools (Read)', tools: [] },
      prometheus: { label: 'Prometheus Metrics Tools', tools: [] },
      loki: { label: 'Loki Log Aggregation Tools', tools: [] },
      tempo: { label: 'Tempo Distributed Tracing Tools', tools: [] },
      github: { label: 'GitHub Version Control Tools', tools: [] },
      argocd: { label: 'ArgoCD Deployment Sync Tools', tools: [] },
      featureflags: { label: 'Feature Flag Toggles', tools: [] },
      topology_qdrant: { label: 'Topology & Runbook Knowledge Tools', tools: [] },
      other: { label: 'Auxiliary System Tools', tools: [] },
    };

    for (const [name, policy] of Object.entries(tools)) {
      if (['restart_pod', 'scale_deployment', 'rollback_release', 'update_resource_limits', 'drain_node'].includes(name)) {
        categories.k8s_write.tools.push({ name, policy });
      } else if (name.startsWith('get_pod_') || name.startsWith('describe_') || name.startsWith('list_') || name === 'get_deployment' || name === 'get_events') {
        categories.k8s_read.tools.push({ name, policy });
      } else if (name.startsWith('prometheus_')) {
        categories.prometheus.tools.push({ name, policy });
      } else if (name.startsWith('loki_')) {
        categories.loki.tools.push({ name, policy });
      } else if (name.startsWith('tempo_')) {
        categories.tempo.tools.push({ name, policy });
      } else if (name.startsWith('github_')) {
        categories.github.tools.push({ name, policy });
      } else if (name.startsWith('argocd_')) {
        categories.argocd.tools.push({ name, policy });
      } else if (name.includes('feature_flag')) {
        categories.featureflags.tools.push({ name, policy });
      } else if (name.startsWith('topology_') || name.startsWith('qdrant_')) {
        categories.topology_qdrant.tools.push({ name, policy });
      } else {
        categories.other.tools.push({ name, policy });
      }
    }
    return categories;
  };

  if (loading) {
    return (
      <div style={{ padding: '0 24px 32px 24px' }}>
        <div className="glass-panel" style={{ padding: '40px', borderRadius: '12px', textAlign: 'center', color: '#94a3b8' }}>
          Loading system and security gateway configurations...
        </div>
      </div>
    );
  }

  const categorizedTools = categorizeTools();

  return (
    <div style={{ padding: '0 24px 32px 24px' }}>
      <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px' }}>
        {/* Header Bar */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <Settings size={22} color="#38bdf8" />
            <h2 style={{ fontSize: '1.25rem', fontWeight: 800, color: '#f8fafc' }}>
              Engine & Safety Gateway Configuration Center
            </h2>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            {saveMessage && (
              <span style={{ fontSize: '0.85rem', color: '#10b981', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '6px' }}>
                <CheckCircle size={16} color="#10b981" />
                {saveMessage}
              </span>
            )}
            {errorMessage && (
              <span style={{ fontSize: '0.85rem', color: '#f43f5e', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '6px' }}>
                <AlertTriangle size={16} color="#f43f5e" />
                {errorMessage}
              </span>
            )}
            <button onClick={loadAll} className="btn btn-sm btn-ghost" title="Refresh configs from server">
              <RefreshCw size={14} />
              <span>Refresh</span>
            </button>
          </div>
        </div>

        {/* Navigation Tabs */}
        <div style={{ display: 'flex', gap: '8px', marginBottom: '24px', borderBottom: '1px solid var(--border-subtle)', paddingBottom: '10px' }}>
          <button
            onClick={() => setActiveTab('agents')}
            className={`btn btn-sm ${activeTab === 'agents' ? 'btn-primary' : 'btn-ghost'}`}
          >
            <Activity size={14} />
            <span>Agent Models & Limits</span>
          </button>
          <button
            onClick={() => setActiveTab('sag')}
            className={`btn btn-sm ${activeTab === 'sag' ? 'btn-primary' : 'btn-ghost'}`}
          >
            <Shield size={14} />
            <span>SAG Tool Policies</span>
          </button>
          <button
            onClick={() => setActiveTab('mcp')}
            className={`btn btn-sm ${activeTab === 'mcp' ? 'btn-primary' : 'btn-ghost'}`}
          >
            <Layers size={14} />
            <span>Registered MCP Servers</span>
          </button>
          <button
            onClick={() => setActiveTab('system')}
            className={`btn btn-sm ${activeTab === 'system' ? 'btn-primary' : 'btn-ghost'}`}
          >
            <Settings size={14} />
            <span>System Topology & Ingestion</span>
          </button>
          <button
            onClick={() => setActiveTab('providers')}
            className={`btn btn-sm ${activeTab === 'providers' ? 'btn-primary' : 'btn-ghost'}`}
          >
            <Key size={14} />
            <span>LLM Provider Keys & Endpoints</span>
          </button>
        </div>

        {/* =================================================================== */}
        {/* TAB 1: AGENTS CONFIGURATION */}
        {/* =================================================================== */}
        {activeTab === 'agents' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                Configure LLM providers, primary reasoning models, fallback chains, and context token ceilings.
              </p>
              <button onClick={() => openHistory('agents')} className="btn btn-sm btn-secondary">
                <RefreshCw size={14} />
                <span>Version History & Rollback</span>
              </button>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: '16px' }}>
              {agentsConfig?.agents &&
                Object.entries(agentsConfig.agents).map(([role, conf]) => {
                  const isEditing = editingAgent === role;
                  return (
                    <div
                      key={role}
                      className="glass-card"
                      style={{
                        padding: '18px',
                        border: isEditing ? '1px solid #38bdf8' : '1px solid var(--border-subtle)',
                        boxShadow: isEditing ? '0 0 16px rgba(56, 189, 248, 0.2)' : 'none',
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                        <div>
                          <h4 className="mono" style={{ fontSize: '0.95rem', fontWeight: 800, color: '#38bdf8' }}>
                            {role}
                          </h4>
                          <span style={{ fontSize: '0.72rem', color: '#94a3b8', textTransform: 'uppercase' }}>
                            Timeout: {conf.timeout_ms || 30000}ms {conf.max_tool_calls ? `• Max Tools: ${conf.max_tool_calls}` : ''}
                          </span>
                        </div>
                        <span className="badge badge-p2">
                          {conf?.primary?.provider}
                        </span>
                      </div>

                      {/* Primary Model Specs */}
                      <div style={{ padding: '10px', backgroundColor: 'rgba(15, 23, 42, 0.6)', borderRadius: '6px', marginBottom: '10px' }}>
                        <div style={{ fontSize: '0.75rem', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', marginBottom: '4px' }}>
                          Primary Model
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span className="mono" style={{ fontSize: '0.85rem', color: '#f8fafc', fontWeight: 600 }}>
                            {conf?.primary?.model}
                          </span>
                          <span className="mono" style={{ fontSize: '0.75rem', color: '#38bdf8' }}>
                            Temp: {conf?.primary?.temperature ?? 0.2}
                          </span>
                        </div>
                        <div style={{ fontSize: '0.75rem', color: '#94a3b8', marginTop: '2px' }}>
                          Token Ceil: <strong className="mono" style={{ color: '#f8fafc' }}>{conf?.primary?.token_limit || '0 (Unbounded)'}</strong>
                        </div>
                      </div>

                      {/* Fallback Chain Overview */}
                      <div style={{ marginBottom: '14px' }}>
                        <div style={{ fontSize: '0.75rem', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', marginBottom: '6px' }}>
                          Fallback Chain ({conf?.fallback?.length || 0})
                        </div>
                        {(!conf?.fallback || conf.fallback.length === 0) ? (
                          <span style={{ fontSize: '0.75rem', color: '#64748b' }}>No fallback models configured.</span>
                        ) : (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                            {conf.fallback.map((fb, idx) => (
                              <div
                                key={idx}
                                style={{
                                  display: 'flex',
                                  justifyContent: 'space-between',
                                  alignItems: 'center',
                                  fontSize: '0.75rem',
                                  padding: '5px 8px',
                                  backgroundColor: 'rgba(15, 23, 42, 0.5)',
                                  borderRadius: '5px',
                                  border: '1px solid rgba(255, 255, 255, 0.05)',
                                }}
                              >
                                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                  <span style={{
                                    fontSize: '0.65rem',
                                    padding: '1px 5px',
                                    borderRadius: '3px',
                                    backgroundColor: fb.provider === 'ollama' ? 'rgba(56, 189, 248, 0.15)' : fb.provider === 'openai' ? 'rgba(34, 197, 94, 0.15)' : fb.provider === 'anthropic' ? 'rgba(245, 158, 11, 0.15)' : 'rgba(168, 85, 247, 0.15)',
                                    color: fb.provider === 'ollama' ? '#38bdf8' : fb.provider === 'openai' ? '#4ade80' : fb.provider === 'anthropic' ? '#fbbf24' : '#c084fc',
                                    fontWeight: 700,
                                    textTransform: 'uppercase',
                                    letterSpacing: '0.03em',
                                  }}>
                                    {fb.provider}
                                  </span>
                                  <span className="mono" style={{ color: '#f1f5f9' }}>
                                    #{idx + 1} {fb.model}
                                  </span>
                                </div>
                                <span className="mono" style={{ color: '#94a3b8' }}>
                                  {fb.token_limit ? `${fb.token_limit} toks` : 'default'}
                                </span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>

                      {/* Action Button */}
                      <button
                        onClick={() => startEditAgent(role, conf)}
                        className="btn btn-sm btn-secondary"
                        style={{ width: '100%', justifyContent: 'center' }}
                      >
                        Edit Role Configuration
                      </button>
                    </div>
                  );
                })}
            </div>

            {/* Agent Edit Drawer / Modal */}
            {editingAgent && agentDraft && (
              <div
                style={{
                  position: 'fixed',
                  inset: 0,
                  backgroundColor: 'rgba(7, 9, 14, 0.85)',
                  backdropFilter: 'blur(8px)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  zIndex: 10000,
                  padding: '20px',
                }}
              >
                <div
                  className="glass-panel"
                  style={{
                    width: '100%',
                    maxWidth: '620px',
                    maxHeight: '90vh',
                    overflowY: 'auto',
                    padding: '24px',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                    <h3 style={{ fontSize: '1.15rem', fontWeight: 800, color: '#f8fafc' }}>
                      Edit Agent: <span className="mono" style={{ color: '#38bdf8' }}>{editingAgent}</span>
                    </h3>
                    <button onClick={() => { setEditingAgent(null); setAgentDraft(null); }} className="btn btn-sm btn-ghost">
                      ✕
                    </button>
                  </div>

                  {/* Primary Model Section */}
                  <div style={{ padding: '14px', backgroundColor: 'rgba(15, 23, 42, 0.6)', borderRadius: '8px', marginBottom: '16px' }}>
                    <h4 style={{ fontSize: '0.85rem', fontWeight: 700, color: '#38bdf8', marginBottom: '10px' }}>
                      Primary Reasoning Model
                    </h4>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: '10px', marginBottom: '10px' }}>
                      <div>
                        <label style={{ display: 'block', fontSize: '0.72rem', color: '#94a3b8', marginBottom: '4px' }}>Provider</label>
                        <select
                          value={agentDraft.primary?.provider || 'gemini'}
                          onChange={(e) => setAgentDraft({
                            ...agentDraft,
                            primary: { ...agentDraft.primary, provider: e.target.value },
                          })}
                          style={{
                            width: '100%',
                            padding: '6px 8px',
                            backgroundColor: '#0f172a',
                            border: '1px solid var(--border-medium)',
                            borderRadius: '4px',
                            color: '#ffffff',
                            fontSize: '0.8rem',
                          }}
                        >
                          <option value="gemini" style={{ backgroundColor: '#0f172a', color: '#ffffff' }}>Google Gemini</option>
                          <option value="openai" style={{ backgroundColor: '#0f172a', color: '#ffffff' }}>OpenAI</option>
                          <option value="anthropic" style={{ backgroundColor: '#0f172a', color: '#ffffff' }}>Anthropic Claude</option>
                          <option value="ollama" style={{ backgroundColor: '#0f172a', color: '#ffffff' }}>Local Ollama</option>
                        </select>
                      </div>

                      <div>
                        <label style={{ display: 'block', fontSize: '0.72rem', color: '#94a3b8', marginBottom: '4px' }}>Model Name</label>
                        <input
                          type="text"
                          value={agentDraft.primary?.model || ''}
                          onChange={(e) => setAgentDraft({
                            ...agentDraft,
                            primary: { ...agentDraft.primary, model: e.target.value },
                          })}
                          style={{
                            width: '100%',
                            padding: '6px 8px',
                            backgroundColor: 'rgba(7, 9, 14, 0.8)',
                            border: '1px solid var(--border-medium)',
                            borderRadius: '4px',
                            color: '#ffffff',
                            fontSize: '0.8rem',
                            fontFamily: 'var(--font-mono)',
                          }}
                        />
                      </div>
                    </div>

                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                      <div>
                        <label style={{ display: 'block', fontSize: '0.72rem', color: '#94a3b8', marginBottom: '4px' }}>Temperature</label>
                        <input
                          type="number"
                          step="0.05"
                          min="0"
                          max="1"
                          value={agentDraft.primary?.temperature ?? 0.2}
                          onChange={(e) => setAgentDraft({
                            ...agentDraft,
                            primary: { ...agentDraft.primary, temperature: parseFloat(e.target.value) },
                          })}
                          style={{
                            width: '100%',
                            padding: '6px 8px',
                            backgroundColor: 'rgba(7, 9, 14, 0.8)',
                            border: '1px solid var(--border-medium)',
                            borderRadius: '4px',
                            color: '#ffffff',
                            fontSize: '0.8rem',
                          }}
                        />
                      </div>

                      <div>
                        <label style={{ display: 'block', fontSize: '0.72rem', color: '#94a3b8', marginBottom: '4px' }}>Token Limit (0 = None)</label>
                        <input
                          type="number"
                          step="500"
                          value={agentDraft.primary?.token_limit ?? 0}
                          onChange={(e) => setAgentDraft({
                            ...agentDraft,
                            primary: { ...agentDraft.primary, token_limit: parseInt(e.target.value, 10) },
                          })}
                          style={{
                            width: '100%',
                            padding: '6px 8px',
                            backgroundColor: 'rgba(7, 9, 14, 0.8)',
                            border: '1px solid var(--border-medium)',
                            borderRadius: '4px',
                            color: '#ffffff',
                            fontSize: '0.8rem',
                            fontFamily: 'var(--font-mono)',
                          }}
                        />
                      </div>
                    </div>

                    <div style={{ marginTop: '10px' }}>
                      <label style={{ display: 'block', fontSize: '0.72rem', color: '#94a3b8', marginBottom: '4px' }}>
                        API Key Override (Optional — leave blank to use global / environment key)
                      </label>
                      <input
                        type="password"
                        placeholder="Leave blank to use global provider key"
                        value={agentDraft.primary?.api_key || ''}
                        onChange={(e) => setAgentDraft({
                          ...agentDraft,
                          primary: { ...agentDraft.primary, api_key: e.target.value },
                        })}
                        style={{
                          width: '100%',
                          padding: '6px 8px',
                          backgroundColor: 'rgba(7, 9, 14, 0.8)',
                          border: '1px solid var(--border-medium)',
                          borderRadius: '4px',
                          color: '#ffffff',
                          fontSize: '0.8rem',
                          fontFamily: 'var(--font-mono)',
                        }}
                      />
                    </div>
                  </div>

                  {/* Fallback Chain Section */}
                  <div style={{ padding: '14px', backgroundColor: 'rgba(15, 23, 42, 0.6)', borderRadius: '8px', marginBottom: '16px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
                      <h4 style={{ fontSize: '0.85rem', fontWeight: 700, color: '#f59e0b' }}>
                        Fallback Chain Models
                      </h4>
                      <button onClick={handleAddFallbackModel} className="btn btn-sm btn-ghost" style={{ fontSize: '0.75rem', color: '#38bdf8' }}>
                        + Add Fallback
                      </button>
                    </div>

                    {(!agentDraft.fallback || agentDraft.fallback.length === 0) ? (
                      <p style={{ fontSize: '0.75rem', color: '#64748b' }}>No fallbacks configured.</p>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                        {agentDraft.fallback.map((fb, idx) => (
                          <div
                            key={idx}
                            style={{
                              padding: '10px 12px',
                              backgroundColor: 'rgba(7, 9, 14, 0.8)',
                              borderRadius: '6px',
                              border: '1px solid var(--border-subtle)',
                              display: 'grid',
                              gridTemplateColumns: '135px 1fr 90px 36px',
                              gap: '10px',
                              alignItems: 'center',
                            }}
                          >
                            <select
                              value={fb.provider || 'gemini'}
                              onChange={(e) => handleUpdateFallback(idx, { provider: e.target.value })}
                              style={{
                                padding: '6px 8px',
                                backgroundColor: '#0f172a',
                                border: '1px solid var(--border-medium)',
                                borderRadius: '4px',
                                color: '#ffffff',
                                fontSize: '0.78rem',
                              }}
                            >
                              <option value="gemini" style={{ backgroundColor: '#0f172a', color: '#ffffff' }}>Google Gemini</option>
                              <option value="openai" style={{ backgroundColor: '#0f172a', color: '#ffffff' }}>OpenAI</option>
                              <option value="anthropic" style={{ backgroundColor: '#0f172a', color: '#ffffff' }}>Anthropic Claude</option>
                              <option value="ollama" style={{ backgroundColor: '#0f172a', color: '#ffffff' }}>Local Ollama</option>
                            </select>

                            <input
                              type="text"
                              value={fb.model || ''}
                              onChange={(e) => handleUpdateFallback(idx, { model: e.target.value })}
                              placeholder={fb.provider === 'ollama' ? 'e.g. qwen2.5:7b' : fb.provider === 'openai' ? 'e.g. gpt-4o-mini' : fb.provider === 'anthropic' ? 'e.g. claude-3-5-haiku-20241022' : 'e.g. gemini-3.1-flash-lite'}
                              style={{
                                padding: '6px 8px',
                                backgroundColor: '#0f172a',
                                border: '1px solid var(--border-medium)',
                                borderRadius: '4px',
                                color: '#ffffff',
                                fontSize: '0.78rem',
                                fontFamily: 'var(--font-mono)',
                              }}
                            />

                            <input
                              type="number"
                              value={fb.token_limit ?? 8000}
                              onChange={(e) => handleUpdateFallback(idx, { token_limit: parseInt(e.target.value, 10) || 0 })}
                              placeholder="limit"
                              title="Token Limit"
                              style={{
                                padding: '6px 8px',
                                backgroundColor: '#0f172a',
                                border: '1px solid var(--border-medium)',
                                borderRadius: '4px',
                                color: '#ffffff',
                                fontSize: '0.78rem',
                                fontFamily: 'var(--font-mono)',
                              }}
                            />

                            <button
                              onClick={() => handleRemoveFallbackModel(idx)}
                              style={{
                                backgroundColor: 'rgba(244, 63, 94, 0.2)',
                                border: '1px solid rgba(244, 63, 94, 0.4)',
                                color: '#fca5a5',
                                borderRadius: '4px',
                                padding: '6px',
                                cursor: 'pointer',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                              }}
                              title="Remove fallback"
                            >
                              ✕
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Modal Footer */}
                  <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
                    <button onClick={() => { setEditingAgent(null); setAgentDraft(null); }} className="btn btn-ghost">
                      Cancel
                    </button>
                    <button onClick={handlePromptSaveAgentDraft} className="btn btn-primary">
                      Save & Persist Configuration
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* =================================================================== */}
        {/* TAB 2: SAG TOOL POLICIES */}
        {/* =================================================================== */}
        {activeTab === 'sag' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', flexWrap: 'wrap', gap: '12px' }}>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                Safety Action Gateway (SAG) tool validation policies, autonomous pre-approval gates, and human approval rules.
              </p>
              <button onClick={() => openHistory('sag')} className="btn btn-sm btn-secondary">
                <RefreshCw size={14} />
                <span>Policy Version History</span>
              </button>
            </div>

            {/* Filter Bar */}
            <div style={{ display: 'flex', gap: '12px', alignItems: 'center', marginBottom: '20px', flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>Category:</span>
                <select
                  value={sagCategoryFilter}
                  onChange={(e) => setSagCategoryFilter(e.target.value)}
                  style={{
                    padding: '6px 12px',
                    backgroundColor: 'rgba(15, 23, 42, 0.8)',
                    border: '1px solid var(--border-medium)',
                    borderRadius: '6px',
                    color: '#ffffff',
                    fontSize: '0.8rem',
                  }}
                >
                  <option value="ALL">All Categories</option>
                  <option value="k8s_write">Kubernetes Mutating (Write)</option>
                  <option value="k8s_read">Kubernetes Inspection (Read)</option>
                  <option value="prometheus">Prometheus Metrics</option>
                  <option value="loki">Loki Logs</option>
                  <option value="tempo">Tempo Traces</option>
                  <option value="github">GitHub VCS</option>
                  <option value="argocd">ArgoCD Deployments</option>
                  <option value="featureflags">Feature Flags</option>
                  <option value="topology_qdrant">Topology & Runbooks</option>
                </select>
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>Status:</span>
                <select
                  value={sagApprovalFilter}
                  onChange={(e) => setSagApprovalFilter(e.target.value)}
                  style={{
                    padding: '6px 12px',
                    backgroundColor: 'rgba(15, 23, 42, 0.8)',
                    border: '1px solid var(--border-medium)',
                    borderRadius: '6px',
                    color: '#ffffff',
                    fontSize: '0.8rem',
                  }}
                >
                  <option value="ALL">All Policies</option>
                  <option value="pre_approved">Autonomous (Pre-Approved)</option>
                  <option value="requires_approval">HITL Approval Required</option>
                </select>
              </div>
            </div>

            {/* Grouped Service Sections */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
              {Object.entries(categorizedTools).map(([catKey, cat]) => {
                if (sagCategoryFilter !== 'ALL' && sagCategoryFilter !== catKey) return null;

                const filteredTools = cat.tools.filter((t) => {
                  if (sagApprovalFilter === 'pre_approved' && !t.policy.pre_approved) return false;
                  if (sagApprovalFilter === 'requires_approval' && !t.policy.requires_approval) return false;
                  return true;
                });

                if (filteredTools.length === 0) return null;

                const preApprovedCount = filteredTools.filter((t) => t.policy.pre_approved).length;
                const requiresApprovalCount = filteredTools.filter((t) => t.policy.requires_approval).length;

                return (
                  <div key={catKey} className="glass-card" style={{ padding: '16px', borderRadius: '10px' }}>
                    {/* Section Header */}
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <Shield size={16} color="#38bdf8" />
                        <h3 style={{ fontSize: '0.95rem', fontWeight: 700, color: '#f8fafc' }}>
                          {cat.label}
                        </h3>
                      </div>
                      <div style={{ display: 'flex', gap: '8px', fontSize: '0.72rem' }}>
                        <span className="badge badge-state">
                          Total: {filteredTools.length}
                        </span>
                        <span className="badge badge-state-resolved">
                          Autonomous: {preApprovedCount}
                        </span>
                        <span className="badge badge-p2">
                          HITL Required: {requiresApprovalCount}
                        </span>
                      </div>
                    </div>

                    {/* Table of Tools */}
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>Tool Identifier</th>
                          <th>Requires Human Approval</th>
                          <th>Pre-Approved (Autonomous)</th>
                          <th style={{ textAlign: 'right' }}>Action</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filteredTools.map(({ name, policy }) => (
                          <tr key={name}>
                            <td>
                              <span className="mono" style={{ fontWeight: 600, color: '#f8fafc' }}>
                                {name}
                              </span>
                            </td>
                            <td>
                              <span className={`badge ${policy.requires_approval ? 'badge-p2' : 'badge-state'}`}>
                                {policy.requires_approval ? 'YES (HITL)' : 'NO (READ)'}
                              </span>
                            </td>
                            <td>
                              <span className={`badge ${policy.pre_approved ? 'badge-state-resolved' : 'badge-state'}`}>
                                {policy.pre_approved ? 'YES (AUTONOMOUS)' : 'NO (BLOCKED/GATE)'}
                              </span>
                            </td>
                            <td style={{ textAlign: 'right' }}>
                              <div style={{ display: 'flex', gap: '6px', justifyContent: 'flex-end' }}>
                                <button
                                  onClick={() => handlePromptTogglePolicy(name, 'pre_approved', !!policy.pre_approved)}
                                  className={`btn btn-sm ${policy.pre_approved ? 'btn-danger' : 'btn-success'}`}
                                  style={{ fontSize: '0.75rem', padding: '4px 10px' }}
                                >
                                  {policy.pre_approved ? 'Revoke Pre-Approval' : 'Grant Pre-Approval'}
                                </button>
                                <button
                                  onClick={() => handlePromptTogglePolicy(name, 'requires_approval', !!policy.requires_approval)}
                                  className="btn btn-sm btn-ghost"
                                  style={{ fontSize: '0.75rem', padding: '4px 8px' }}
                                  title="Toggle requires_approval gate"
                                >
                                  {policy.requires_approval ? 'Set As Read' : 'Set As Gate'}
                                </button>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* =================================================================== */}
        {/* TAB 3: REGISTERED MCP SERVERS */}
        {/* =================================================================== */}
        {activeTab === 'mcp' && (() => {
          const mcpServerEntries = mcpConfig?.mcp_servers
            ? Object.entries(mcpConfig.mcp_servers as Record<string, MCPServerConfig>)
            : [];
          return (
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <p style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                  Active Model Context Protocol (FastMCP) servers, endpoint URLs, and registered agent tools.
                </p>
                <button onClick={() => openRawEditor('mcp')} className="btn btn-sm btn-secondary">
                  <Settings size={14} />
                  <span>Edit Raw Configuration</span>
                </button>
              </div>

              {mcpServerEntries.length === 0 ? (
                <div className="glass-card" style={{ padding: '24px', borderRadius: '8px', textAlign: 'center', color: '#94a3b8', fontSize: '0.85rem' }}>
                  No MCP servers configured or unable to load MCP configuration.
                </div>
              ) : (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: '16px' }}>
                  {mcpServerEntries.map(([name, srv]) => {
                    const endpointDisplay =
                      srv?.transport === 'stdio' && srv?.command
                        ? `stdio: ${srv.command}${srv.args?.length ? ` ${srv.args.join(' ')}` : ''}`
                        : srv?.server_url || '—';
                    const toolsList = Array.isArray(srv?.tools) ? srv.tools : [];

                    return (
                      <div key={name} className="glass-card" style={{ padding: '16px', borderRadius: '8px' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <Layers size={16} color="#38bdf8" />
                            <h4 className="mono" style={{ fontSize: '0.9rem', fontWeight: 800, color: '#f8fafc' }}>
                              {name}
                            </h4>
                          </div>
                          <span className="badge badge-state-resolved">
                            CONFIGURED
                          </span>
                        </div>

                        <div style={{ fontSize: '0.75rem', color: '#94a3b8', marginBottom: '8px' }}>
                          Endpoint URL:
                          <div className="mono" style={{ color: '#38bdf8', marginTop: '2px', wordBreak: 'break-all' }}>
                            {endpointDisplay}
                          </div>
                        </div>

                        <div style={{ marginBottom: '8px' }}>
                          <span style={{ fontSize: '0.72rem', color: '#94a3b8', textTransform: 'uppercase' }}>
                            Registered Tools ({toolsList.length}):
                          </span>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginTop: '4px' }}>
                            {toolsList.map((t, idx) => {
                              const toolName = typeof t === 'string' ? t : t?.name || JSON.stringify(t);
                              const toolTags = typeof t === 'string' ? [] : Array.isArray(t?.tags) ? t.tags : [];
                              return (
                                <span
                                  key={`${toolName}-${idx}`}
                                  className="mono"
                                  title={toolTags.length > 0 ? `Tags: ${toolTags.join(', ')}` : undefined}
                                  style={{ fontSize: '0.72rem', padding: '2px 6px', backgroundColor: 'rgba(56, 189, 248, 0.1)', color: '#bae6fd', borderRadius: '4px' }}
                                >
                                  {toolName}
                                </span>
                              );
                            })}
                          </div>
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.72rem', color: '#cbd5e1', borderTop: '1px solid var(--border-subtle)', paddingTop: '8px' }}>
                          <Key size={12} color="#f59e0b" />
                          <span>Auth: {['github', 'kubernetes', 'argocd'].includes(name) ? 'Environment Token Configured' : 'Local / Network Allowed'}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })()}

        {/* =================================================================== */}
        {/* TAB 4: SYSTEM TOPOLOGY & INGESTION CONFIG */}
        {/* =================================================================== */}
        {activeTab === 'system' && (
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
              <p style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                System-level runtime configuration, SLO convergence monitors, alert ingestion schemas, and notification triggers.
              </p>
              <button onClick={() => openRawEditor('system')} className="btn btn-sm btn-secondary">
                <Settings size={14} />
                <span>Edit Raw Configuration</span>
              </button>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: '16px' }}>
              {/* Card 1: Core System & Control Plane */}
              <div className="glass-card" style={{ padding: '16px' }}>
                <h4 style={{ fontSize: '0.85rem', fontWeight: 800, color: '#38bdf8', marginBottom: '8px' }}>
                  Core Control Plane
                </h4>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '0.8rem' }}>
                  <div>Environment: <strong style={{ color: '#f8fafc' }}>{systemConfig?.system?.environment || 'development'}</strong></div>
                  <div>Log Level: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.system?.log_level || 'INFO'}</strong></div>
                  <div>API Port: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.system?.api_port || 8080}</strong></div>
                  <div>Control Plane URL: <code className="mono" style={{ color: '#38bdf8' }}>{systemConfig?.system?.api_url || 'http://localhost:8080'}</code></div>
                </div>
              </div>

              {/* Card 2: State Store & Memory */}
              <div className="glass-card" style={{ padding: '16px' }}>
                <h4 style={{ fontSize: '0.85rem', fontWeight: 800, color: '#10b981', marginBottom: '8px' }}>
                  Redis State & Queue Store
                </h4>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '0.8rem' }}>
                  <div>URL: <code className="mono" style={{ color: '#38bdf8' }}>{systemConfig?.redis?.url || 'redis://localhost:6379'}</code></div>
                  <div>Database: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.redis?.db ?? 0}</strong></div>
                  <div>Health: <span className="badge badge-state-resolved">HEALTHY</span></div>
                </div>
              </div>

              {/* Card 3: Vector Database (Qdrant) */}
              <div className="glass-card" style={{ padding: '16px' }}>
                <h4 style={{ fontSize: '0.85rem', fontWeight: 800, color: '#c084fc', marginBottom: '8px' }}>
                  Qdrant Vector Database
                </h4>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '0.8rem' }}>
                  <div>URL: <code className="mono" style={{ color: '#38bdf8' }}>{systemConfig?.qdrant?.url || 'http://localhost:6333'}</code></div>
                  <div>Collection: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.qdrant?.collection || 'aegis_runbooks'}</strong></div>
                  <div>Embedding Provider: <strong style={{ color: '#f8fafc' }}>{systemConfig?.qdrant?.embedding_provider || 'ollama'}</strong></div>
                  <div>Model: <code className="mono" style={{ color: '#cbd5e1' }}>{systemConfig?.qdrant?.embedding_model || 'qwen3-embedding'}</code></div>
                </div>
              </div>

              {/* Card 4: SLO Convergence Monitor */}
              <div className="glass-card" style={{ padding: '16px' }}>
                <h4 style={{ fontSize: '0.85rem', fontWeight: 800, color: '#f59e0b', marginBottom: '8px' }}>
                  SLO Convergence Monitor
                </h4>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '0.8rem' }}>
                  <div>Stateless Bake: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.convergence?.bake_time_seconds_stateless || 60}s</strong></div>
                  <div>Stateful Bake: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.convergence?.bake_time_seconds_stateful || 120}s</strong></div>
                  <div>Poll Interval: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.convergence?.poll_interval_seconds || 30}s</strong></div>
                  <div>Error Rate Threshold: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.convergence?.slo?.error_rate_threshold ?? 0.01}</strong></div>
                  <div>P99 Latency Limit: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.convergence?.slo?.p99_latency_ms ?? 500}ms</strong></div>
                </div>
              </div>

              {/* Card 5: Telegram Bot Notifications */}
              <div className="glass-card" style={{ padding: '16px' }}>
                <h4 style={{ fontSize: '0.85rem', fontWeight: 800, color: '#38bdf8', marginBottom: '8px' }}>
                  Telegram Notifications
                </h4>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '0.8rem' }}>
                  <div>Status: <span className={`badge ${systemConfig?.telegram?.enabled ? 'badge-state-resolved' : 'badge-state'}`}>{systemConfig?.telegram?.enabled ? 'ENABLED' : 'DISABLED'}</span></div>
                  <div>Bot Service URL: <code className="mono" style={{ color: '#38bdf8' }}>{systemConfig?.telegram?.bot_url || 'http://localhost:8088'}</code></div>
                  <div>Port: <strong className="mono" style={{ color: '#f8fafc' }}>{systemConfig?.telegram?.service_port || 8088}</strong></div>
                  <div>Triggers: <span style={{ color: '#94a3b8' }}>Creation, Triage, Approval, Escalation, Closure</span></div>
                </div>
              </div>
            </div>

            {/* Registered Entities & Identity Registry (Secrets Never Exposed) */}
            {identitiesConfig && (
              <div className="glass-card" style={{ padding: '16px', marginTop: '16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                  <div>
                    <h4 style={{ fontSize: '0.9rem', fontWeight: 800, color: '#38bdf8' }}>
                      Registered Identities &amp; Scoped Access Registry
                    </h4>
                    <p style={{ fontSize: '0.75rem', color: '#94a3b8', marginTop: '2px' }}>
                      Multi-operator keys, Telegram user bindings, webhook emitters (port 8080), and Docker-internal Aegis services (port 8081).
                    </p>
                  </div>
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '12px' }}>
                  <div>
                    <div style={{ fontSize: '0.72rem', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '6px' }}>
                      Operators ({identitiesConfig.operators.length})
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                      {identitiesConfig.operators.map((op) => (
                        <div key={op.id} style={{ padding: '8px 10px', borderRadius: '6px', backgroundColor: 'rgba(15, 23, 42, 0.6)', border: '1px solid var(--border-subtle)', fontSize: '0.76rem' }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <strong className="mono" style={{ color: '#f8fafc' }}>{op.username}</strong>
                            <span className={`badge ${op.key_configured ? 'badge-state-resolved' : 'badge-state'}`}>
                              {op.role.toUpperCase()} • {op.key_configured ? 'KEY SET' : 'NO KEY'}
                            </span>
                          </div>
                          <div style={{ color: '#94a3b8', marginTop: '3px', fontSize: '0.72rem' }}>
                            Key Env: <code className="mono">{op.api_key_env || '—'}</code>
                            {op.telegram_user_id_env
                              ? ` • TG Env: ${op.telegram_user_id_env} (${op.telegram_configured ? 'LINKED' : 'UNSET'})`
                              : op.telegram_user_id
                                ? ` • TG ID: ${op.telegram_user_id}`
                                : ''}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div>
                    <div style={{ fontSize: '0.72rem', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '6px' }}>
                      Webhooks &amp; External Clients ({identitiesConfig.webhooks.length + identitiesConfig.external_clients.length})
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '12px' }}>
                      {identitiesConfig.webhooks.map((wh) => (
                        <div key={wh.id} style={{ padding: '8px 10px', borderRadius: '6px', backgroundColor: 'rgba(15, 23, 42, 0.6)', border: '1px solid var(--border-subtle)', fontSize: '0.76rem' }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <strong className="mono" style={{ color: '#f8fafc' }}>{wh.name}</strong>
                            <span className={`badge ${wh.key_configured ? 'badge-state-resolved' : 'badge-state'}`}>
                              WEBHOOK • {wh.key_configured ? 'KEY SET' : 'NO KEY'}
                            </span>
                          </div>
                          <div style={{ color: '#94a3b8', marginTop: '3px', fontSize: '0.72rem' }}>
                            Scopes: <code className="mono">{wh.scopes.join(', ')}</code> ({wh.api_key_env || '—'})
                          </div>
                        </div>
                      ))}
                      {identitiesConfig.external_clients.map((ext) => (
                        <div key={ext.id} style={{ padding: '8px 10px', borderRadius: '6px', backgroundColor: 'rgba(15, 23, 42, 0.6)', border: '1px solid var(--border-subtle)', fontSize: '0.76rem' }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <strong className="mono" style={{ color: '#f8fafc' }}>{ext.name}</strong>
                            <span className={`badge ${ext.key_configured ? 'badge-state-resolved' : 'badge-state'}`}>
                              CLIENT • {ext.key_configured ? 'KEY SET' : 'NO KEY'}
                            </span>
                          </div>
                          <div style={{ color: '#94a3b8', marginTop: '3px', fontSize: '0.72rem' }}>
                            Scopes: <code className="mono">{ext.scopes.join(', ')}</code> ({ext.api_key_env || '—'})
                          </div>
                        </div>
                      ))}
                    </div>

                    <div style={{ fontSize: '0.72rem', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '6px' }}>
                      Internal Aegis Services (Port 8081 • No API Key Required)
                    </div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
                      {identitiesConfig.internal_services.map((svc) => (
                        <span key={svc} className="mono" style={{ fontSize: '0.72rem', padding: '2px 7px', backgroundColor: 'rgba(16, 185, 129, 0.12)', color: '#a7f3d0', borderRadius: '4px' }}>
                          {svc}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* SHA-256 Digest Sync & Operator-Gated Drift Control */}
            <div className="glass-card" style={{ padding: '16px', marginTop: '16px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <div>
                  <h4 style={{ fontSize: '0.9rem', fontWeight: 800, color: '#f59e0b' }}>
                    Topology &amp; Runbook Digest Versioning (SHA-256)
                  </h4>
                  <p style={{ fontSize: '0.75rem', color: '#94a3b8', marginTop: '2px' }}>
                    Detects drift without silently overwriting; approve pending drift or trigger manual/force rebuild.
                  </p>
                </div>
                <button
                  type="button"
                  onClick={async () => {
                    setDriftBusy('check');
                    try {
                      const chk = await checkDrift();
                      const updated = await getDriftStatus();
                      setDriftStatus(updated);
                      const hasPending = Boolean(updated?.pending?.topology || updated?.pending?.runbooks);
                      setDriftNotice(
                        hasPending
                          ? 'Drift check completed: pending drift staged for review.'
                          : `Drift check completed: topology & runbooks are in sync (${chk?.checked?.join(', ') || 'verified'}).`
                      );
                    } catch (err: any) {
                      setDriftNotice(err.message || 'Drift check failed', true);
                    } finally {
                      setDriftBusy(null);
                    }
                  }}
                  disabled={driftBusy !== null}
                  className="btn btn-sm btn-secondary"
                >
                  <RefreshCw size={13} />
                  <span>{driftBusy === 'check' ? 'Checking...' : 'Check Drift'}</span>
                </button>
              </div>

              {driftFeedback && (
                <div
                  style={{
                    marginBottom: '12px',
                    padding: '8px 12px',
                    borderRadius: '6px',
                    fontSize: '0.76rem',
                    fontWeight: 600,
                    backgroundColor: driftFeedback.isError ? 'rgba(244, 63, 94, 0.14)' : 'rgba(16, 185, 129, 0.14)',
                    border: driftFeedback.isError ? '1px solid rgba(244, 63, 94, 0.35)' : '1px solid rgba(16, 185, 129, 0.35)',
                    color: driftFeedback.isError ? '#fda4af' : '#6ee7b7',
                  }}
                >
                  {driftFeedback.text}
                </div>
              )}

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: '12px' }}>
                {(['topology', 'runbooks'] as const).map((targetKey) => {
                  const meta = targetKey === 'topology' ? driftStatus?.topology_meta : driftStatus?.runbooks_meta;
                  const pending = driftStatus?.pending?.[targetKey] || null;
                  return (
                    <div key={targetKey} style={{ padding: '12px', borderRadius: '6px', backgroundColor: 'rgba(15, 23, 42, 0.6)', border: pending ? '1px solid rgba(245, 158, 11, 0.45)' : '1px solid var(--border-subtle)', fontSize: '0.78rem' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                        <strong style={{ textTransform: 'uppercase', color: '#f8fafc' }}>{targetKey}</strong>
                        <span className={`badge ${pending ? 'badge-state-escalated' : 'badge-state-resolved'}`}>
                          {pending ? 'DRIFT PENDING' : `v${meta?.version ?? 1} IN SYNC`}
                        </span>
                      </div>
                      <div style={{ color: '#94a3b8', fontSize: '0.73rem', marginBottom: '8px' }}>
                        <div>Active SHA: <code className="mono" style={{ color: '#38bdf8' }}>{meta?.sha256 ? meta.sha256.slice(0, 12) : '—'}</code></div>
                        {meta?.updated_at && <div>Updated: <span className="mono">{new Date(meta.updated_at).toLocaleString()}</span></div>}
                      </div>

                      {pending && (
                        <div style={{ padding: '8px', borderRadius: '4px', backgroundColor: 'rgba(245, 158, 11, 0.1)', marginBottom: '8px', color: '#fde68a', fontSize: '0.72rem' }}>
                          Pending: <code className="mono">{(pending.old_sha || 'none').slice(0, 10)} → {pending.new_sha.slice(0, 10)}</code>
                          <div style={{ display: 'flex', gap: '6px', marginTop: '6px' }}>
                            <button
                              type="button"
                              onClick={async () => {
                                setDriftBusy(`${targetKey}-approve`);
                                try {
                                  const r = await approveDrift(targetKey);
                                  setDriftStatus(await getDriftStatus());
                                  setDriftNotice(
                                    `Approved ${targetKey} drift sync (v${r.version ?? 1}, sha ${(r.sha256 || '').slice(0, 10) || 'updated'}).`
                                  );
                                } catch (err: any) {
                                  const refreshed = await getDriftStatus().catch(() => null);
                                  if (refreshed) setDriftStatus(refreshed);
                                  setDriftNotice(err.message || 'Approve failed', true);
                                } finally {
                                  setDriftBusy(null);
                                }
                              }}
                              disabled={driftBusy !== null}
                              className="btn btn-sm btn-success"
                            >
                              {driftBusy === `${targetKey}-approve` ? 'Approving...' : 'Approve Drift'}
                            </button>
                            <button
                              type="button"
                              onClick={async () => {
                                setDriftBusy(`${targetKey}-reject`);
                                try {
                                  await rejectDrift(targetKey);
                                  setDriftStatus(await getDriftStatus());
                                  setDriftNotice(`Dismissed pending drift for ${targetKey}.`);
                                } catch (err: any) {
                                  const refreshed = await getDriftStatus().catch(() => null);
                                  if (refreshed) setDriftStatus(refreshed);
                                  setDriftNotice(err.message || 'Reject failed', true);
                                } finally {
                                  setDriftBusy(null);
                                }
                              }}
                              disabled={driftBusy !== null}
                              className="btn btn-sm btn-danger"
                            >
                              {driftBusy === `${targetKey}-reject` ? 'Rejecting...' : 'Reject'}
                            </button>
                          </div>
                        </div>
                      )}

                      <div style={{ display: 'flex', gap: '8px' }}>
                        <button
                          type="button"
                          onClick={async () => {
                            setDriftBusy(`${targetKey}-sync`);
                            try {
                              const r = await syncDriftTarget(targetKey, false);
                              setDriftStatus(await getDriftStatus());
                              const syncLabel = r.status || (r.changed ? 'synced' : 'unchanged');
                              const shaShort = r.sha256 ? r.sha256.slice(0, 10) : 'n/a';
                              setDriftNotice(`${targetKey} sync: ${syncLabel} (v${r.version ?? 1}, sha ${shaShort}).`);
                            } catch (err: any) {
                              setDriftNotice(err.message || 'Sync failed', true);
                            } finally {
                              setDriftBusy(null);
                            }
                          }}
                          disabled={driftBusy !== null}
                          className="btn btn-sm btn-secondary"
                        >
                          {driftBusy === `${targetKey}-sync` ? 'Syncing...' : 'Manual Sync'}
                        </button>
                        <button
                          type="button"
                          onClick={async () => {
                            setDriftBusy(`${targetKey}-force`);
                            try {
                              const r = await syncDriftTarget(targetKey, true);
                              setDriftStatus(await getDriftStatus());
                              const shaShort = r.sha256 ? r.sha256.slice(0, 10) : 'n/a';
                              setDriftNotice(`${targetKey} force rebuilt (v${r.version ?? 1}, sha ${shaShort}).`);
                            } catch (err: any) {
                              setDriftNotice(err.message || 'Force rebuild failed', true);
                            } finally {
                              setDriftBusy(null);
                            }
                          }}
                          disabled={driftBusy !== null}
                          className="btn btn-sm btn-ghost"
                          style={{ border: '1px solid var(--border-subtle)' }}
                        >
                          {driftBusy === `${targetKey}-force` ? 'Rebuilding...' : 'Force Rebuild'}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        )}

        {/* =================================================================== */}
        {/* TAB 5: LLM PROVIDER KEYS & ENDPOINTS */}
        {/* =================================================================== */}
        {activeTab === 'providers' && (
          <div>
            <div style={{ marginBottom: '20px' }}>
              <h3 style={{ fontSize: '1.05rem', fontWeight: 800, color: '#f8fafc', marginBottom: '6px' }}>
                LLM Provider API Keys & Serving Endpoints
              </h3>
              <p style={{ fontSize: '0.825rem', color: '#94a3b8' }}>
                Configure global credentials for Gemini, OpenAI, Anthropic, and local Ollama instances. Keys entered here are saved into system configuration and automatically picked up by all agent reasoning loops without requiring container restarts.
              </p>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))', gap: '20px', marginBottom: '24px' }}>
              {/* Google Gemini Card */}
              <div className="glass-card" style={{ padding: '20px', borderRadius: '8px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Key size={18} color="#38bdf8" />
                    <h4 style={{ fontSize: '0.95rem', fontWeight: 700, color: '#f8fafc' }}>Google Gemini</h4>
                  </div>
                  <span className={`badge ${providerConfig?.google_api_key_set ? 'badge-state-resolved' : 'badge-state'}`}>
                    {providerConfig?.google_api_key_set ? 'CONFIGURED' : 'NOT SET'}
                  </span>
                </div>
                <p style={{ fontSize: '0.775rem', color: '#94a3b8', marginBottom: '12px' }}>
                  Current Active Key: <code className="mono" style={{ color: providerConfig?.google_api_key_set ? '#10b981' : '#f43f5e' }}>{providerConfig?.google_api_key_masked || 'None'}</code>
                </p>
                <div>
                  <label style={{ display: 'block', fontSize: '0.72rem', color: '#cbd5e1', marginBottom: '6px' }}>
                    Update Google API Key (starts with AIza...)
                  </label>
                  <input
                    type="password"
                    placeholder="Enter new Google API Key..."
                    value={googleKeyInput}
                    onChange={(e) => setGoogleKeyInput(e.target.value)}
                    style={{
                      width: '100%',
                      padding: '8px 10px',
                      backgroundColor: 'rgba(7, 9, 14, 0.8)',
                      border: '1px solid var(--border-medium)',
                      borderRadius: '6px',
                      color: '#ffffff',
                      fontSize: '0.85rem',
                      fontFamily: 'var(--font-mono)',
                    }}
                  />
                </div>
              </div>

              {/* OpenAI Card */}
              <div className="glass-card" style={{ padding: '20px', borderRadius: '8px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Key size={18} color="#10b981" />
                    <h4 style={{ fontSize: '0.95rem', fontWeight: 700, color: '#f8fafc' }}>OpenAI</h4>
                  </div>
                  <span className={`badge ${providerConfig?.openai_api_key_set ? 'badge-state-resolved' : 'badge-state'}`}>
                    {providerConfig?.openai_api_key_set ? 'CONFIGURED' : 'NOT SET'}
                  </span>
                </div>
                <p style={{ fontSize: '0.775rem', color: '#94a3b8', marginBottom: '12px' }}>
                  Current Active Key: <code className="mono" style={{ color: providerConfig?.openai_api_key_set ? '#10b981' : '#f43f5e' }}>{providerConfig?.openai_api_key_masked || 'None'}</code>
                </p>
                <div>
                  <label style={{ display: 'block', fontSize: '0.72rem', color: '#cbd5e1', marginBottom: '6px' }}>
                    Update OpenAI API Key (starts with sk-...)
                  </label>
                  <input
                    type="password"
                    placeholder="Enter new OpenAI API Key..."
                    value={openaiKeyInput}
                    onChange={(e) => setOpenaiKeyInput(e.target.value)}
                    style={{
                      width: '100%',
                      padding: '8px 10px',
                      backgroundColor: 'rgba(7, 9, 14, 0.8)',
                      border: '1px solid var(--border-medium)',
                      borderRadius: '6px',
                      color: '#ffffff',
                      fontSize: '0.85rem',
                      fontFamily: 'var(--font-mono)',
                    }}
                  />
                </div>
              </div>

              {/* Anthropic Card */}
              <div className="glass-card" style={{ padding: '20px', borderRadius: '8px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Key size={18} color="#f59e0b" />
                    <h4 style={{ fontSize: '0.95rem', fontWeight: 700, color: '#f8fafc' }}>Anthropic</h4>
                  </div>
                  <span className={`badge ${providerConfig?.anthropic_api_key_set ? 'badge-state-resolved' : 'badge-state'}`}>
                    {providerConfig?.anthropic_api_key_set ? 'CONFIGURED' : 'NOT SET'}
                  </span>
                </div>
                <p style={{ fontSize: '0.775rem', color: '#94a3b8', marginBottom: '12px' }}>
                  Current Active Key: <code className="mono" style={{ color: providerConfig?.anthropic_api_key_set ? '#10b981' : '#f43f5e' }}>{providerConfig?.anthropic_api_key_masked || 'None'}</code>
                </p>
                <div>
                  <label style={{ display: 'block', fontSize: '0.72rem', color: '#cbd5e1', marginBottom: '6px' }}>
                    Update Anthropic API Key (starts with sk-ant-...)
                  </label>
                  <input
                    type="password"
                    placeholder="Enter new Anthropic API Key..."
                    value={anthropicKeyInput}
                    onChange={(e) => setAnthropicKeyInput(e.target.value)}
                    style={{
                      width: '100%',
                      padding: '8px 10px',
                      backgroundColor: 'rgba(7, 9, 14, 0.8)',
                      border: '1px solid var(--border-medium)',
                      borderRadius: '6px',
                      color: '#ffffff',
                      fontSize: '0.85rem',
                      fontFamily: 'var(--font-mono)',
                    }}
                  />
                </div>
              </div>

              {/* Ollama Local URL Card */}
              <div className="glass-card" style={{ padding: '20px', borderRadius: '8px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <Layers size={18} color="#c084fc" />
                    <h4 style={{ fontSize: '0.95rem', fontWeight: 700, color: '#f8fafc' }}>Ollama Serving URL</h4>
                  </div>
                  <span className="badge badge-state-resolved">LOCAL INFERENCE</span>
                </div>
                <p style={{ fontSize: '0.775rem', color: '#94a3b8', marginBottom: '12px' }}>
                  Current Endpoint: <code className="mono" style={{ color: '#38bdf8' }}>{providerConfig?.ollama_url || 'http://localhost:11434'}</code>
                </p>
                <div>
                  <label style={{ display: 'block', fontSize: '0.72rem', color: '#cbd5e1', marginBottom: '6px' }}>
                    Ollama Base URL (e.g. http://host.docker.internal:11434)
                  </label>
                  <input
                    type="text"
                    placeholder="http://host.docker.internal:11434"
                    value={ollamaUrlInput}
                    onChange={(e) => setOllamaUrlInput(e.target.value)}
                    style={{
                      width: '100%',
                      padding: '8px 10px',
                      backgroundColor: 'rgba(7, 9, 14, 0.8)',
                      border: '1px solid var(--border-medium)',
                      borderRadius: '6px',
                      color: '#ffffff',
                      fontSize: '0.85rem',
                      fontFamily: 'var(--font-mono)',
                    }}
                  />
                </div>
              </div>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
              <button
                onClick={handlePromptSaveProviders}
                disabled={savingProviders}
                className="btn btn-primary"
                style={{ padding: '8px 24px', fontSize: '0.85rem' }}
              >
                {savingProviders ? 'Saving Settings...' : 'Save Provider Credentials'}
              </button>
            </div>
          </div>
        )}

        {/* =================================================================== */}
        {/* VERSION HISTORY & ROLLBACK MODAL */}
        {/* =================================================================== */}
        {historyModalOpen && (
          <div
            style={{
              position: 'fixed',
              inset: 0,
              backgroundColor: 'rgba(7, 9, 14, 0.85)',
              backdropFilter: 'blur(8px)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10000,
              padding: '20px',
            }}
          >
            <div className="glass-panel" style={{ width: '100%', maxWidth: '640px', maxHeight: '80vh', overflowY: 'auto', padding: '24px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <h3 style={{ fontSize: '1.1rem', fontWeight: 800, color: '#f8fafc' }}>
                  Version History & Rollback: <span className="mono" style={{ color: '#38bdf8' }}>{historyTarget}.yaml</span>
                </h3>
                <button onClick={() => setHistoryModalOpen(false)} className="btn btn-sm btn-ghost">✕</button>
              </div>

              {historyLoading ? (
                <p style={{ color: '#94a3b8', textAlign: 'center', padding: '20px' }}>Loading version snapshots...</p>
              ) : historyList.length === 0 ? (
                <p style={{ color: '#94a3b8', textAlign: 'center', padding: '20px' }}>No previous versions recorded yet. Changes will be automatically snapshotted upon modification.</p>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                  {historyList.map((ver) => {
                    const isExpanded = !!expandedVersions[ver.version_id];
                    const details = versionDetailsMap[ver.version_id];
                    const isLoadingThis = loadingVersionId === ver.version_id;

                    return (
                      <div
                        key={ver.version_id}
                        style={{
                          backgroundColor: 'rgba(15, 23, 42, 0.7)',
                          border: isExpanded ? '1px solid #38bdf8' : '1px solid var(--border-subtle)',
                          borderRadius: '8px',
                          overflow: 'hidden',
                          transition: 'all 0.2s ease',
                        }}
                      >
                        {/* Header Row */}
                        <div
                          style={{
                            padding: '12px 16px',
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            cursor: 'pointer',
                            userSelect: 'none',
                          }}
                          onClick={() => handleToggleVersionDetails(ver.version_id)}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                            <div style={{
                              color: '#38bdf8',
                              fontSize: '0.85rem',
                              fontWeight: 700,
                              transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                              transition: 'transform 0.2s ease',
                              display: 'inline-block',
                            }}>
                              ▶
                            </div>
                            <div>
                              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                <span className="mono" style={{ fontSize: '0.85rem', fontWeight: 700, color: '#38bdf8' }}>
                                  {ver.version_id}
                                </span>
                                <span className="badge badge-state" style={{ fontSize: '0.675rem' }}>
                                  {ver.file}
                                </span>
                              </div>
                              <div style={{ fontSize: '0.75rem', color: '#94a3b8', marginTop: '3px' }}>
                                {new Date(ver.timestamp).toLocaleString()} • {ver.description}
                              </div>
                            </div>
                          </div>

                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }} onClick={(e) => e.stopPropagation()}>
                            <button
                              onClick={() => handleToggleVersionDetails(ver.version_id)}
                              className="btn btn-sm btn-ghost"
                              style={{ fontSize: '0.75rem', padding: '4px 8px' }}
                              title="Inspect diff and changes"
                            >
                              {isExpanded ? 'Hide Diff' : 'View Diff'}
                            </button>
                            <button
                              onClick={() => handleInitiateRollback(ver.version_id)}
                              className="btn btn-sm btn-danger"
                              style={{ fontSize: '0.75rem', padding: '4px 10px' }}
                              title="Restore this version snapshot"
                            >
                              Restore This Version
                            </button>
                          </div>
                        </div>

                        {/* Expandable Section: What Actually Happened & Diff */}
                        {isExpanded && (
                          <div style={{
                            padding: '12px 16px 16px 16px',
                            borderTop: '1px solid var(--border-subtle)',
                            backgroundColor: 'rgba(7, 9, 14, 0.5)',
                          }}>
                            <div style={{ marginBottom: '8px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                              <span style={{ fontSize: '0.75rem', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                                Unified Diff vs Current Active Configuration
                              </span>
                              {isLoadingThis && (
                                <span style={{ fontSize: '0.725rem', color: '#38bdf8' }}>
                                  Loading snapshot diff...
                                </span>
                              )}
                            </div>

                            {isLoadingThis ? (
                              <div style={{ padding: '20px', textAlign: 'center', color: '#94a3b8', fontSize: '0.8rem' }}>
                                Fetching snapshot and generating diff...
                              </div>
                            ) : details ? (
                              renderDiffLines(details.diff)
                            ) : (
                              <div style={{ padding: '14px', textAlign: 'center', color: '#94a3b8', fontSize: '0.8rem' }}>
                                Unable to load diff preview for this version.
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}

        {/* =================================================================== */}
        {/* RAW JSON / YAML MODAL EDITOR */}
        {/* =================================================================== */}
        {rawEditorOpen && (
          <div
            style={{
              position: 'fixed',
              inset: 0,
              backgroundColor: 'rgba(7, 9, 14, 0.85)',
              backdropFilter: 'blur(8px)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 10000,
              padding: '20px',
            }}
          >
            <div className="glass-panel" style={{ width: '100%', maxWidth: '720px', maxHeight: '85vh', display: 'flex', flexDirection: 'column', padding: '24px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <h3 style={{ fontSize: '1.1rem', fontWeight: 800, color: '#f8fafc' }}>
                  Raw Editor: <span className="mono" style={{ color: '#38bdf8' }}>{rawEditorTarget}.yaml</span>
                </h3>
                <button onClick={() => setRawEditorOpen(false)} className="btn btn-sm btn-ghost">✕</button>
              </div>

              <textarea
                value={rawContent}
                onChange={(e) => setRawContent(e.target.value)}
                rows={18}
                style={{
                  width: '100%',
                  padding: '12px',
                  backgroundColor: 'rgba(7, 9, 14, 0.9)',
                  border: '1px solid var(--border-medium)',
                  borderRadius: '6px',
                  color: '#bae6fd',
                  fontFamily: 'var(--font-mono)',
                  fontSize: '0.8rem',
                  outline: 'none',
                  resize: 'vertical',
                  flexGrow: 1,
                  marginBottom: '16px',
                }}
              />

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
                <button onClick={() => setRawEditorOpen(false)} className="btn btn-ghost">
                  Cancel
                </button>
                <button onClick={handlePromptSaveRawContent} className="btn btn-primary">
                  Validate & Save Configuration
                </button>
              </div>
            </div>
          </div>
        )}

        {/* =================================================================== */}
        {/* 1. ROLLBACK SECOND CONFIRMATION MODAL */}
        {/* =================================================================== */}
        {confirmRollbackModal && typeof document !== 'undefined' && createPortal(
          <div style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: 'rgba(7, 9, 14, 0.88)',
            backdropFilter: 'blur(10px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 10005,
            padding: '20px',
          }}>
            <div className="glass-panel" style={{ width: '100%', maxWidth: '580px', maxHeight: '85vh', overflowY: 'auto', padding: '24px', border: '1px solid rgba(239, 68, 68, 0.5)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
                <AlertTriangle size={22} color="#ef4444" />
                <h3 style={{ fontSize: '1.15rem', fontWeight: 800, color: '#fca5a5' }}>
                  Confirm Configuration Rollback
                </h3>
              </div>

              <p style={{ fontSize: '0.825rem', color: '#cbd5e1', lineHeight: 1.5, marginBottom: '16px' }}>
                You are about to restore <strong className="mono" style={{ color: '#38bdf8' }}>{historyTarget}.yaml</strong> to snapshot <strong className="mono" style={{ color: '#38bdf8' }}>{confirmRollbackModal.versionId}</strong>.
                A backup of the current state will be created automatically before the rollback is written.
              </p>

              {/* What the value will be now after this update */}
              <div style={{
                backgroundColor: 'rgba(15, 23, 42, 0.8)',
                border: '1px solid var(--border-medium)',
                borderRadius: '8px',
                padding: '14px',
                marginBottom: '16px',
              }}>
                <div style={{ fontSize: '0.75rem', fontWeight: 700, color: '#38bdf8', textTransform: 'uppercase', marginBottom: '8px' }}>
                  Value After This Update (Target Snapshot):
                </div>
                <div style={{ fontSize: '0.775rem', color: '#94a3b8', marginBottom: '6px' }}>
                  Snapshot Description: <strong style={{ color: '#f8fafc' }}>{confirmRollbackModal.details?.description || 'Historical backup'}</strong>
                </div>
                <div style={{ fontSize: '0.775rem', color: '#94a3b8', marginBottom: '10px' }}>
                  Recorded At: <strong style={{ color: '#f8fafc' }}>{confirmRollbackModal.details?.timestamp ? new Date(confirmRollbackModal.details.timestamp).toLocaleString() : '—'}</strong>
                </div>

                <div style={{ fontSize: '0.725rem', color: '#94a3b8', marginBottom: '6px', fontWeight: 600 }}>
                  Restoration Diff Preview (Lines with '+' will become active):
                </div>
                {confirmRollbackModal.details?.diff ? (
                  renderDiffLines(confirmRollbackModal.details.diff)
                ) : (
                  <div style={{ padding: '8px', color: '#94a3b8', fontSize: '0.75rem', fontStyle: 'italic' }}>
                    Restores exact content from snapshot {confirmRollbackModal.versionId}.
                  </div>
                )}
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
                <button
                  onClick={() => setConfirmRollbackModal(null)}
                  className="btn btn-ghost"
                  disabled={rollingBack}
                >
                  Cancel
                </button>
                <button
                  onClick={executeRollback}
                  className="btn btn-danger"
                  disabled={rollingBack}
                  style={{ backgroundColor: '#dc2626' }}
                >
                  {rollingBack ? 'Restoring Configuration...' : 'Confirm & Restore Version'}
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}

        {/* =================================================================== */}
        {/* 2. AGENT OLD -> NEW CONFIRMATION MODAL */}
        {/* =================================================================== */}
        {confirmAgentModal && typeof document !== 'undefined' && createPortal(
          <div style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: 'rgba(7, 9, 14, 0.88)',
            backdropFilter: 'blur(10px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 10005,
            padding: '20px',
          }}>
            <div className="glass-panel" style={{ width: '100%', maxWidth: '580px', maxHeight: '85vh', overflowY: 'auto', padding: '24px', border: '1px solid rgba(56, 189, 248, 0.4)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
                <Activity size={22} color="#38bdf8" />
                <h3 style={{ fontSize: '1.15rem', fontWeight: 800, color: '#f8fafc' }}>
                  Confirm Agent Update: <span className="mono" style={{ color: '#38bdf8' }}>{confirmAgentModal.role}</span>
                </h3>
              </div>

              <p style={{ fontSize: '0.825rem', color: '#cbd5e1', lineHeight: 1.5, marginBottom: '16px' }}>
                Please review the modified parameters below before saving. A version snapshot of <strong className="mono">agents.yaml</strong> will be automatically archived.
              </p>

              <div style={{
                backgroundColor: 'rgba(15, 23, 42, 0.8)',
                border: '1px solid var(--border-medium)',
                borderRadius: '8px',
                padding: '14px',
                marginBottom: '18px',
              }}>
                <div style={{ fontSize: '0.75rem', fontWeight: 700, color: '#38bdf8', textTransform: 'uppercase', marginBottom: '10px' }}>
                  Field Changes (Old → New):
                </div>

                {confirmAgentModal.diffs.length === 0 ? (
                  <div style={{ padding: '8px', color: '#94a3b8', fontSize: '0.8rem', fontStyle: 'italic' }}>
                    No field values were modified.
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {confirmAgentModal.diffs.map((d, i) => (
                      <div key={i} style={{
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '4px',
                        padding: '8px 10px',
                        backgroundColor: 'rgba(7, 9, 14, 0.6)',
                        borderRadius: '6px',
                        border: '1px solid var(--border-subtle)',
                      }}>
                        <span style={{ fontSize: '0.75rem', fontWeight: 700, color: '#cbd5e1' }}>
                          {d.field}
                        </span>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.775rem' }}>
                          <span style={{ color: '#fca5a5', textDecoration: 'line-through' }}>
                            {d.oldVal}
                          </span>
                          <span style={{ color: '#94a3b8' }}>→</span>
                          <span style={{ color: '#86efac', fontWeight: 600 }}>
                            {d.newVal}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
                <button
                  onClick={() => setConfirmAgentModal(null)}
                  className="btn btn-ghost"
                  disabled={savingAction}
                >
                  Go Back / Edit
                </button>
                <button
                  onClick={executeSaveAgentDraft}
                  className="btn btn-primary"
                  disabled={savingAction}
                >
                  {savingAction ? 'Saving...' : 'Confirm & Apply Update'}
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}

        {/* =================================================================== */}
        {/* 3. SAG POLICY OLD -> NEW CONFIRMATION MODAL */}
        {/* =================================================================== */}
        {confirmSagModal && typeof document !== 'undefined' && createPortal(
          <div style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: 'rgba(7, 9, 14, 0.88)',
            backdropFilter: 'blur(10px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 10005,
            padding: '20px',
          }}>
            <div className="glass-panel" style={{ width: '100%', maxWidth: '480px', padding: '24px', border: '1px solid rgba(245, 158, 11, 0.4)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
                <Shield size={22} color="#f59e0b" />
                <h3 style={{ fontSize: '1.1rem', fontWeight: 800, color: '#f8fafc' }}>
                  Confirm SAG Security Policy Change
                </h3>
              </div>

              <p style={{ fontSize: '0.825rem', color: '#cbd5e1', lineHeight: 1.5, marginBottom: '16px' }}>
                Updating validation policy for tool <strong className="mono" style={{ color: '#38bdf8' }}>{confirmSagModal.toolName}</strong>. A version snapshot of <strong className="mono">sag_policies.yaml</strong> will be automatically recorded.
              </p>

              <div style={{
                backgroundColor: 'rgba(15, 23, 42, 0.8)',
                border: '1px solid var(--border-medium)',
                borderRadius: '8px',
                padding: '14px',
                marginBottom: '16px',
              }}>
                <div style={{ fontSize: '0.75rem', fontWeight: 700, color: '#f59e0b', textTransform: 'uppercase', marginBottom: '8px' }}>
                  Policy Update (Old → New):
                </div>
                <div style={{ fontSize: '0.8rem', marginBottom: '6px' }}>
                  Property: <strong className="mono" style={{ color: '#cbd5e1' }}>{confirmSagModal.field}</strong>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '0.85rem' }}>
                  <span className={`badge ${confirmSagModal.oldVal ? 'badge-state-resolved' : 'badge-state'}`}>
                    OLD: {confirmSagModal.oldVal ? 'TRUE (Enabled)' : 'FALSE (Disabled)'}
                  </span>
                  <span style={{ color: '#94a3b8' }}>→</span>
                  <span className={`badge ${confirmSagModal.newVal ? 'badge-state-resolved' : 'badge-state'}`} style={{ fontWeight: 800 }}>
                    NEW: {confirmSagModal.newVal ? 'TRUE (Enabled)' : 'FALSE (Disabled)'}
                  </span>
                </div>
                <div style={{ fontSize: '0.75rem', color: '#94a3b8', marginTop: '10px', lineHeight: 1.4 }}>
                  {confirmSagModal.field === 'pre_approved'
                    ? (confirmSagModal.newVal
                        ? 'ℹ️ Granting pre-approval whitelists this tool for automated execution during incidents.'
                        : 'ℹ️ Revoking pre-approval blocks automated agent selection of this tool.')
                    : (confirmSagModal.newVal
                        ? 'ℹ️ Setting requires_approval enforces Rule 9 human sign-off before any execution.'
                        : 'ℹ️ Disabling requires_approval permits autonomous execution if pre-approved.')}
                </div>
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
                <button
                  onClick={() => setConfirmSagModal(null)}
                  className="btn btn-ghost"
                  disabled={savingAction}
                >
                  Cancel
                </button>
                <button
                  onClick={executeToggleSagPolicy}
                  className="btn btn-warning"
                  disabled={savingAction}
                >
                  {savingAction ? 'Updating...' : 'Confirm Policy Update'}
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}

        {/* =================================================================== */}
        {/* 4. PROVIDER CREDENTIALS OLD -> NEW CONFIRMATION MODAL */}
        {/* =================================================================== */}
        {confirmProviderModal && typeof document !== 'undefined' && createPortal(
          <div style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: 'rgba(7, 9, 14, 0.88)',
            backdropFilter: 'blur(10px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 10005,
            padding: '20px',
          }}>
            <div className="glass-panel" style={{ width: '100%', maxWidth: '520px', padding: '24px', border: '1px solid rgba(56, 189, 248, 0.4)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
                <Key size={22} color="#38bdf8" />
                <h3 style={{ fontSize: '1.15rem', fontWeight: 800, color: '#f8fafc' }}>
                  Confirm LLM Provider Update
                </h3>
              </div>

              <p style={{ fontSize: '0.825rem', color: '#cbd5e1', lineHeight: 1.5, marginBottom: '16px' }}>
                The following credentials and endpoint changes will be written into system configuration:
              </p>

              <div style={{
                backgroundColor: 'rgba(15, 23, 42, 0.8)',
                border: '1px solid var(--border-medium)',
                borderRadius: '8px',
                padding: '14px',
                marginBottom: '18px',
              }}>
                <div style={{ fontSize: '0.75rem', fontWeight: 700, color: '#38bdf8', textTransform: 'uppercase', marginBottom: '10px' }}>
                  Parameters To Be Updated (Old → New):
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {confirmProviderModal.diffs.map((d, i) => (
                    <div key={i} style={{
                      padding: '8px 10px',
                      backgroundColor: 'rgba(7, 9, 14, 0.6)',
                      borderRadius: '6px',
                      border: '1px solid var(--border-subtle)',
                    }}>
                      <span style={{ fontSize: '0.75rem', fontWeight: 700, color: '#cbd5e1' }}>
                        {d.field}
                      </span>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.775rem', marginTop: '3px' }}>
                        <span style={{ color: '#fca5a5' }}>{d.oldVal}</span>
                        <span style={{ color: '#94a3b8' }}>→</span>
                        <span style={{ color: '#86efac', fontWeight: 600 }}>{d.newVal}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
                <button
                  onClick={() => setConfirmProviderModal(null)}
                  className="btn btn-ghost"
                  disabled={savingProviders}
                >
                  Cancel
                </button>
                <button
                  onClick={executeSaveProviders}
                  className="btn btn-primary"
                  disabled={savingProviders}
                >
                  {savingProviders ? 'Persisting...' : 'Confirm & Save Credentials'}
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}

        {/* =================================================================== */}
        {/* 5. RAW EDITOR OLD -> NEW CONFIRMATION MODAL */}
        {/* =================================================================== */}
        {confirmRawModal && typeof document !== 'undefined' && createPortal(
          <div style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: 'rgba(7, 9, 14, 0.88)',
            backdropFilter: 'blur(10px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 10005,
            padding: '20px',
          }}>
            <div className="glass-panel" style={{ width: '100%', maxWidth: '520px', padding: '24px', border: '1px solid rgba(56, 189, 248, 0.4)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
                <Settings size={22} color="#38bdf8" />
                <h3 style={{ fontSize: '1.15rem', fontWeight: 800, color: '#f8fafc' }}>
                  Confirm Raw Configuration Update: <span className="mono" style={{ color: '#38bdf8' }}>{confirmRawModal.target}.yaml</span>
                </h3>
              </div>

              <p style={{ fontSize: '0.825rem', color: '#cbd5e1', lineHeight: 1.5, marginBottom: '16px' }}>
                You are about to overwrite <strong className="mono">{confirmRawModal.target}.yaml</strong>.
                Schema validation and a historical backup snapshot will be generated.
              </p>

              <div style={{
                backgroundColor: 'rgba(15, 23, 42, 0.8)',
                border: '1px solid var(--border-medium)',
                borderRadius: '8px',
                padding: '14px',
                marginBottom: '18px',
              }}>
                <div style={{ fontSize: '0.75rem', fontWeight: 700, color: '#38bdf8', textTransform: 'uppercase', marginBottom: '8px' }}>
                  Top-Level Configuration Sections Modified:
                </div>
                {confirmRawModal.diffSummary.length === 0 ? (
                  <div style={{ color: '#94a3b8', fontSize: '0.775rem' }}>No top-level section changes detected.</div>
                ) : (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                    {confirmRawModal.diffSummary.map((d, i) => (
                      <span key={i} className={`badge ${d.status === 'added' ? 'badge-state-resolved' : d.status === 'removed' ? 'badge-p1' : 'badge-p2'}`}>
                        {d.key} ({d.status})
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
                <button
                  onClick={() => setConfirmRawModal(null)}
                  className="btn btn-ghost"
                  disabled={savingAction}
                >
                  Cancel
                </button>
                <button
                  onClick={executeSaveRawContent}
                  className="btn btn-primary"
                  disabled={savingAction}
                >
                  {savingAction ? 'Saving...' : 'Confirm & Persist Configuration'}
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}
      </div>
    </div>
  );
};
