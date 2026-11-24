export type Severity = 'P1' | 'P2' | 'P3';

export type IncidentState =
  | 'NEW'
  | 'GROUPING'
  | 'TRIAGE'
  | 'MERGING'
  | 'REMEDIATION'
  | 'AWAITING_APPROVAL'
  | 'EXECUTING'
  | 'MONITORING'
  | 'REPORTING'
  | 'RESOLVED'
  | 'CLOSED'
  | 'ESCALATED'
  | 'STANDBY';

export interface Incident {
  incident_id: string;
  state: IncidentState;
  severity: Severity;
  affected_service: string;
  affected_namespace?: string;
  created_at: string;
  updated_at: string;
  retry_count: number;
  has_report?: boolean;
  visited_states?: IncidentState[];
  last_transition?: {
    from: IncidentState;
    to: IncidentState;
    timestamp?: string;
  };
  context?: {
    primary_hypothesis?: string;
    confidence?: number;
    corroboration_score?: number;
    affected_services?: string[];
    ranked_evidence?: Array<{
      signal_type: string;
      description: string;
      peak_value?: string | number;
      source?: string;
    }>;
    verbatim_signals?: Record<string, any>;
    merged_findings?: any;
    latest_steering?: string;
    [key: string]: any;
  };
}

export interface LedgerEntry {
  entry_id: string;
  incident_id: string;
  timestamp: string;
  state: IncidentState;
  event_type: string;
  source: string;
  details?: Record<string, any>;
}

export interface RunbookCitation {
  runbook_id?: string;
  book_name?: string;
  section?: string;
  line_range?: string;
  chunk_id?: string;
  referenced_text?: string;
}

export interface ActionPlanItem {
  tool_name: string;
  parameters: Record<string, any>;
  target_resource?: string;
  rationale?: string;
  description?: string;
  runbook_citations?: RunbookCitation[];
}

export interface ActionProposal {
  action_id?: string;
  actions: ActionPlanItem[];
  rationale?: string;
  risk_assessment?: string;
  confidence: number;
  pre_approved?: boolean;
  reversible?: boolean;
  runbook_citations?: RunbookCitation[];
}

export interface ActionRecord {
  action_id: string;
  proposal: ActionProposal;
  runbook_citations?: RunbookCitation[];
  status: 'pending' | 'approved' | 'denied' | 'steered' | 'timed_out';
  channel?: string;
  decided_by?: string;
  decided_by_meta?: Record<string, any>;
  auth_key_id?: string;
  decided_at?: string;
  reason?: string;
  instruction?: string;
  timeout_seconds?: number;
  expires_at?: string;
}

export interface AuthPrincipal {
  id: string;
  username: string;
  display_name: string;
  entity_type: 'operator' | 'webhook' | 'external_client' | 'internal_service';
  role: string;
  scopes: string[];
  key_id?: string;
  telegram_user_id?: string;
  channel?: string;
}

export interface IdentitiesPublicResponse {
  operators: Array<{
    id: string;
    username: string;
    display_name: string;
    role: string;
    scopes: string[];
    telegram_user_id?: string | null;
    telegram_user_id_env?: string | null;
    telegram_configured?: boolean;
    api_key_env?: string | null;
    key_configured: boolean;
    enabled: boolean;
  }>;
  webhooks: Array<{
    id: string;
    name: string;
    source_type: string;
    scopes: string[];
    api_key_env?: string | null;
    key_configured: boolean;
    enabled: boolean;
  }>;
  external_clients: Array<{
    id: string;
    name: string;
    scopes: string[];
    api_key_env?: string | null;
    key_configured: boolean;
    enabled: boolean;
  }>;
  internal_services: string[];
}

export interface AuditInteractionEntry {
  id: string;
  timestamp: string;
  direction: string;
  category: string;
  actor: string;
  entity_type: string;
  key_id: string;
  credential_ref: string;
  target: string;
  action: string;
  external: boolean;
  status: string;
  incident_id: string;
  latency_ms?: number | null;
  details: Record<string, any>;
}

export interface DriftRecord {
  target: 'topology' | 'runbooks';
  status: string;
  old_sha: string;
  new_sha: string;
  current_version: number;
  summary: Record<string, any>;
  detected_at: string;
}

export interface DriftStatusResponse {
  topology_meta: {
    version?: number;
    sha256?: string;
    updated_at?: string;
    updated_by?: string;
    node_count?: number;
    edge_count?: number;
  };
  runbooks_meta: {
    version?: number;
    sha256?: string;
    updated_at?: string;
    updated_by?: string;
    entry_count?: number;
  };
  pending: {
    topology: DriftRecord | null;
    runbooks: DriftRecord | null;
  };
}

export interface DroppedAlertEntry {
  id: string;
  timestamp: string;
  alertname: string;
  service: string;
  namespace: string;
  allowed_namespaces: string[];
  fingerprint: string;
  ingested_by: string;
  key_id: string;
  reason: string;
}

export interface TopologyNode {
  id: string;
  name: string;
  kind?: string;
  type?: string;
  namespace?: string;
  ports?: number[];
  resource_limits?: Record<string, any>;
  is_stub?: boolean;
}

export interface TopologyEdge {
  from_node?: string;
  to_node?: string;
  from?: string;
  to?: string;
  source?: string;
  target?: string;
  protocol?: string;
  port?: number;
}

export interface TopologyGraph {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  namespaces?: string[];
  observed_namespaces?: string[];
  current_namespace?: string;
}

export interface SystemStatus {
  status: 'operational' | 'paused';
  paused: boolean;
  details?: {
    actor?: string;
    reason?: string;
    paused_at?: string;
    resumed_at?: string;
  };
  server_time: string;
}

export interface ToolPolicy {
  tool_name?: string;
  pre_approved: boolean;
  requires_approval: boolean;
  confidence_threshold?: number;
  allowed_environments?: string[];
  aliases?: string[];
  [key: string]: any;
}

export interface SagPoliciesConfig {
  policies?: {
    default_requires_approval?: boolean;
    tools?: Record<string, ToolPolicy>;
    confidence_thresholds?: Record<string, number>;
    [key: string]: any;
  };
  tools?: Record<string, ToolPolicy>;
  confidence_thresholds?: Record<string, number>;
}

export interface ModelConfig {
  provider: string;
  model: string;
  token_limit?: number;
  temperature?: number;
  api_key?: string;
  base_url?: string;
}

export interface AgentRoleConfig {
  primary: ModelConfig;
  fallback?: ModelConfig[];
  timeout_ms?: number;
  max_tool_calls?: number;
  retry_on?: string[];
  [key: string]: any;
}

export interface AgentsConfig {
  agents: Record<string, AgentRoleConfig>;
}

export interface ConfigVersionItem {
  version_id: string;
  file: string;
  timestamp: string;
  description: string;
  path: string;
}

export interface VersionDetails {
  version_id: string;
  file: string;
  timestamp: string;
  description: string;
  raw_content: string;
  current_content: string;
  diff: string;
  parsed: any;
  current_parsed: any;
}

export interface MCPToolItemConfig {
  name: string;
  tags?: string[];
}

export type MCPToolEntry = string | MCPToolItemConfig;

export interface MCPServerConfig {
  server_url?: string;
  transport?: 'streamable_http' | 'http' | 'sse' | 'stdio';
  command?: string;
  args?: string[];
  tools: MCPToolEntry[];
  auth_configured?: boolean;
  [key: string]: any;
}

export interface IncidentReportResponse {
  incident_id: string;
  exists: boolean;
  content: string | null;
  path?: string;
  message?: string;
}

