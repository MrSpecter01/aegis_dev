from enum import Enum


class IncidentState(str, Enum):
    NEW = "NEW"
    GROUPING = "GROUPING"
    TRIAGE = "TRIAGE"
    MERGING = "MERGING"
    REMEDIATION = "REMEDIATION"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    MONITORING = "MONITORING"
    REPORTING = "REPORTING"
    CLOSED = "CLOSED"
    ESCALATED = "ESCALATED"
    STANDBY = "STANDBY"


class Severity(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class AgentRole(str, Enum):
    INFRASTRUCTURE_INVESTIGATOR = "infrastructure_investigator"
    LOG_TRACE_INVESTIGATOR = "log_trace_investigator"
    CHANGE_CONFIG_INVESTIGATOR = "change_config_investigator"
    REMEDIATION_AGENT = "remediation"
    SCRIBE_AGENT = "scribe"


class ToolCategory(str, Enum):
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    TEMPO = "tempo"
    KUBERNETES = "kubernetes"
    ARGOCD = "argocd"
    GITHUB = "github"
    FEATURE_FLAGS = "featureflags"
    QDRANT = "qdrant"
    TOPOLOGY = "topology"
