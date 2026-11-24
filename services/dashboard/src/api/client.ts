import {
  Incident,
  LedgerEntry,
  ActionRecord,
  TopologyGraph,
  SystemStatus,
  SagPoliciesConfig,
  AgentsConfig,
  ConfigVersionItem,
  VersionDetails,
  IncidentReportResponse,
  AuthPrincipal,
  IdentitiesPublicResponse,
  AuditInteractionEntry,
  DriftStatusResponse,
  DroppedAlertEntry,
} from '../types';

const API_BASE = '/v1';
const AUTH_KEY_STORAGE = 'aegis_api_key';
const AUTH_USER_STORAGE = 'aegis_auth_user';

type AuthListener = () => void;
const authListeners: Set<AuthListener> = new Set();

export function onAuthRequired(listener: AuthListener): () => void {
  authListeners.add(listener);
  return () => authListeners.delete(listener);
}

export function getApiKey(): string {
  return sessionStorage.getItem(AUTH_KEY_STORAGE) || '';
}

export function setApiKey(key: string): void {
  if (key) {
    sessionStorage.setItem(AUTH_KEY_STORAGE, key.trim());
  } else {
    sessionStorage.removeItem(AUTH_KEY_STORAGE);
    sessionStorage.removeItem(AUTH_USER_STORAGE);
  }
}

export function getStoredUser(): AuthPrincipal | null {
  try {
    const raw = sessionStorage.getItem(AUTH_USER_STORAGE);
    return raw ? (JSON.parse(raw) as AuthPrincipal) : null;
  } catch {
    return null;
  }
}

export function setStoredUser(user: AuthPrincipal | null): void {
  if (user) {
    sessionStorage.setItem(AUTH_USER_STORAGE, JSON.stringify(user));
  } else {
    sessionStorage.removeItem(AUTH_USER_STORAGE);
  }
}

export function getCurrentUsername(): string | undefined {
  const u = getStoredUser();
  return u?.username || undefined;
}

export function clearApiKey(): void {
  sessionStorage.removeItem(AUTH_KEY_STORAGE);
  sessionStorage.removeItem(AUTH_USER_STORAGE);
  authListeners.forEach((fn) => fn());
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const apiKey = getApiKey();
  const headers = new Headers(options.headers || {});
  
  if (apiKey) {
    headers.set('X-API-Key', apiKey);
  }
  if (!headers.has('Content-Type') && options.body && typeof options.body === 'string') {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers,
  });

  if (response.status === 401 || response.status === 403) {
    clearApiKey();
    throw new Error(`Authentication Required (${response.status}): Invalid or missing API key`);
  }

  if (!response.ok) {
    let errorDetail = `Request failed: ${response.status} ${response.statusText}`;
    try {
      const errJson = await response.json();
      if (errJson.detail) {
        errorDetail = errJson.detail;
      }
    } catch {
      // fallback to statusText
    }
    throw new Error(errorDetail);
  }

  return response.json() as Promise<T>;
}

// Fleet & Incident APIs
export async function getIncidents(state?: string, severity?: string): Promise<Incident[]> {
  const params = new URLSearchParams();
  if (state && state !== 'ALL') params.append('state', state);
  if (severity && severity !== 'ALL') params.append('severity', severity);
  const q = params.toString() ? `?${params.toString()}` : '';
  return request<Incident[]>(`/incidents${q}`);
}

export async function getIncident(incidentId: string): Promise<Incident> {
  return request<Incident>(`/incidents/${encodeURIComponent(incidentId)}`);
}

export async function getIncidentLedger(incidentId: string): Promise<LedgerEntry[]> {
  return request<LedgerEntry[]>(`/incidents/${encodeURIComponent(incidentId)}/ledger`);
}

export async function getIncidentAction(incidentId: string, actionId?: string): Promise<ActionRecord> {
  const q = actionId ? `?action_id=${encodeURIComponent(actionId)}` : '';
  return request<ActionRecord>(`/incidents/${encodeURIComponent(incidentId)}/action${q}`);
}

export async function getIncidentReport(incidentId: string): Promise<IncidentReportResponse> {
  return request<IncidentReportResponse>(`/incidents/${encodeURIComponent(incidentId)}/report`);
}

// Auth & Identity APIs
export async function getCurrentUser(): Promise<AuthPrincipal> {
  return request<AuthPrincipal>('/auth/me');
}

export async function getIdentitiesConfig(): Promise<IdentitiesPublicResponse> {
  return request<IdentitiesPublicResponse>('/config/identities');
}

export async function getAuditInteractions(params?: {
  limit?: number;
  direction?: string;
  externalOnly?: boolean;
  incidentId?: string;
}): Promise<{ count: number; items: AuditInteractionEntry[] }> {
  const qs = new URLSearchParams();
  if (params?.limit) qs.append('limit', String(params.limit));
  if (params?.direction) qs.append('direction', params.direction);
  if (params?.externalOnly) qs.append('external_only', 'true');
  if (params?.incidentId) qs.append('incident_id', params.incidentId);
  const q = qs.toString() ? `?${qs.toString()}` : '';
  return request(`/audit/interactions${q}`);
}

// Approval & Intervention APIs
export async function approveAction(
  incidentId: string,
  actionId: string,
  decision: 'approved' | 'denied' | 'steer',
  reason?: string,
  instruction?: string
): Promise<any> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/approve`, {
    method: 'POST',
    body: JSON.stringify({
      action_id: actionId,
      decision,
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
      reason,
      instruction,
    }),
  });
}

export async function steerIncident(
  incidentId: string,
  instruction: string,
  actionId?: string
): Promise<any> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/steer`, {
    method: 'POST',
    body: JSON.stringify({
      instruction,
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
      action_id: actionId,
    }),
  });
}

export async function pauseIncident(incidentId: string, reason: string): Promise<any> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/pause`, {
    method: 'POST',
    body: JSON.stringify({
      reason,
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
    }),
  });
}

export async function resumeIncident(incidentId: string, instruction?: string): Promise<any> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/resume`, {
    method: 'POST',
    body: JSON.stringify({
      instruction,
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
    }),
  });
}

export async function escalateIncident(incidentId: string, reason: string): Promise<any> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/escalate`, {
    method: 'POST',
    body: JSON.stringify({
      reason,
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
    }),
  });
}

export async function closeIncident(incidentId: string, reason: string, generateReport: boolean = false): Promise<any> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/close`, {
    method: 'POST',
    body: JSON.stringify({
      reason,
      generate_report: generateReport,
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
    }),
  });
}

export async function dropIncident(incidentId: string, reason?: string): Promise<any> {
  return request(`/incidents/${encodeURIComponent(incidentId)}/drop`, {
    method: 'POST',
    body: JSON.stringify({
      reason: reason || 'Incident dropped by operator',
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
    }),
  });
}

// Topology APIs
export async function getTopology(namespace?: string): Promise<TopologyGraph> {
  const query = namespace && namespace !== 'ALL' ? `?namespace=${encodeURIComponent(namespace)}` : '';
  return request<TopologyGraph>(`/topology${query}`);
}

export async function refreshTopology(force: boolean = false): Promise<{
  status: string;
  changed?: boolean;
  sha256?: string;
  version?: number;
  node_count: number;
  edge_count: number;
}> {
  return request(`/topology/refresh${force ? '?force=true' : ''}`, { method: 'POST' });
}

// Drift & SHA-256 Digest Sync APIs
export async function getDriftStatus(): Promise<DriftStatusResponse> {
  return request<DriftStatusResponse>('/drift/status');
}

export async function checkDrift(target?: 'topology' | 'runbooks'): Promise<any> {
  const q = target ? `?target=${encodeURIComponent(target)}` : '';
  return request(`/drift/check${q}`, { method: 'POST' });
}

export async function approveDrift(target: 'topology' | 'runbooks'): Promise<any> {
  return request(`/drift/${encodeURIComponent(target)}/approve`, {
    method: 'POST',
    body: JSON.stringify({
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
    }),
  });
}

export async function rejectDrift(target: 'topology' | 'runbooks', reason?: string): Promise<any> {
  return request(`/drift/${encodeURIComponent(target)}/reject`, {
    method: 'POST',
    body: JSON.stringify({
      reason: reason || 'Rejected from dashboard',
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
    }),
  });
}

export async function syncDriftTarget(target: 'topology' | 'runbooks', force: boolean = false): Promise<any> {
  return request(`/drift/${encodeURIComponent(target)}/sync${force ? '?force=true' : ''}`, {
    method: 'POST',
    body: JSON.stringify({
      force,
      actor: getCurrentUsername() || 'operator',
      channel: 'dashboard',
    }),
  });
}

export async function getDroppedAlerts(limit: number = 50): Promise<{ count: number; items: DroppedAlertEntry[] }> {
  return request(`/alerts/dropped?limit=${limit}`);
}

// System Status, Emergency Halt & Demo APIs
export async function getSystemStatus(): Promise<SystemStatus> {
  return request<SystemStatus>('/system/status');
}

export async function pauseSystem(reason: string, actor?: string): Promise<any> {
  return request('/system/pause', {
    method: 'POST',
    body: JSON.stringify({ reason, actor: actor || getCurrentUsername() || 'operator' }),
  });
}

export async function resumeSystem(actor?: string): Promise<any> {
  return request('/system/resume', {
    method: 'POST',
    body: JSON.stringify({ actor: actor || getCurrentUsername() || 'operator' }),
  });
}

export async function triggerDemoIncident(): Promise<any> {
  return request('/system/demo-incident', { method: 'POST' });
}

// Configuration APIs
export async function getSystemConfig(): Promise<any> {
  return request('/config');
}

export async function updateSystemConfig(config: any): Promise<any> {
  return request('/config/system', {
    method: 'PUT',
    body: JSON.stringify(config),
  });
}

export async function getProviderConfig(): Promise<any> {
  return request('/config/providers');
}

export async function updateProviderConfig(providers: any): Promise<any> {
  return request('/config/providers', {
    method: 'PUT',
    body: JSON.stringify(providers),
  });
}

export async function getAgentsConfig(): Promise<AgentsConfig> {
  return request<AgentsConfig>('/config/agents');
}

export async function updateAgentConfig(agentId: string, config: any): Promise<any> {
  return request(`/config/agents/${encodeURIComponent(agentId)}`, {
    method: 'PUT',
    body: JSON.stringify(config),
  });
}

export async function updateAllAgents(config: any): Promise<any> {
  return request('/config/agents', {
    method: 'PUT',
    body: JSON.stringify(config),
  });
}

export async function getAgentsHistory(): Promise<ConfigVersionItem[]> {
  return request<ConfigVersionItem[]>('/config/agents/history');
}

export async function rollbackAgentsConfig(versionId: string): Promise<any> {
  return request('/config/agents/rollback', {
    method: 'POST',
    body: JSON.stringify({ version_id: versionId }),
  });
}

export async function getSagConfig(): Promise<SagPoliciesConfig> {
  return request<SagPoliciesConfig>('/config/sag');
}

export async function updateSagToolPolicy(toolName: string, policy: any): Promise<any> {
  return request(`/config/sag/tools/${encodeURIComponent(toolName)}`, {
    method: 'PUT',
    body: JSON.stringify(policy),
  });
}

export async function updateAllSag(config: any): Promise<any> {
  return request('/config/sag', {
    method: 'PUT',
    body: JSON.stringify(config),
  });
}

export async function getSagHistory(): Promise<ConfigVersionItem[]> {
  return request<ConfigVersionItem[]>('/config/sag/history');
}

export async function rollbackSagConfig(versionId: string): Promise<any> {
  return request('/config/sag/rollback', {
    method: 'POST',
    body: JSON.stringify({ version_id: versionId }),
  });
}

export async function getMcpConfig(): Promise<any> {
  return request('/config/mcp');
}

export async function updateMcpConfig(config: any): Promise<any> {
  return request('/config/mcp', {
    method: 'PUT',
    body: JSON.stringify(config),
  });
}

export async function getVersionDetails(versionId: string): Promise<VersionDetails> {
  return request<VersionDetails>(`/config/history/version/${encodeURIComponent(versionId)}`);
}

