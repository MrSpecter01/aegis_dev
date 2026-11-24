import React, { useState, useEffect } from 'react';
import { Incident, LedgerEntry, ActionRecord, TopologyGraph, IncidentReportResponse } from '../types';
import {
  getIncident,
  getIncidentLedger,
  getIncidentAction,
  getIncidentReport,
  approveAction,
  pauseIncident,
  resumeIncident,
  escalateIncident,
  closeIncident,
  dropIncident,
  steerIncident,
  getTopology,
} from '../api/client';
import { ArrowLeft, RefreshCw } from './Icons';
import { StateIndicator } from './StateIndicator';
import { ActionControls } from './ActionControls';
import { ApprovalQueue } from './ApprovalQueue';
import { FindingsSummary } from './FindingsSummary';
import { TopologyDiagram } from './TopologyDiagram';
import { LedgerTimeline } from './LedgerTimeline';
import { IncidentReportViewer } from './IncidentReportViewer';

interface IncidentDetailProps {
  incidentId: string;
  onBack: () => void;
  isEmergencyHalted?: boolean;
}

export const IncidentDetail: React.FC<IncidentDetailProps> = ({ incidentId, onBack, isEmergencyHalted }) => {
  const [incident, setIncident] = useState<Incident | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [actionRecord, setActionRecord] = useState<ActionRecord | null>(null);
  const [topology, setTopology] = useState<TopologyGraph | null>(null);
  const [reportData, setReportData] = useState<IncidentReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'overview' | 'ledger' | 'topology' | 'report'>('overview');

  const fetchDetail = async () => {
    try {
      const [incData, ledgerData] = await Promise.all([
        getIncident(incidentId),
        getIncidentLedger(incidentId),
      ]);
      setIncident(incData);
      setLedger(ledgerData);

      // Attempt to fetch action record if applicable
      try {
        const actData = await getIncidentAction(incidentId);
        setActionRecord(actData);
      } catch {
        setActionRecord(null);
      }

      // Fetch topology
      try {
        const topData = await getTopology(incData.affected_namespace);
        setTopology(topData);
      } catch {
        // topology optional
      }

      // Fetch incident report if available
      try {
        const rep = await getIncidentReport(incidentId);
        setReportData(rep);
      } catch {
        setReportData(null);
      }
    } catch (err) {
      console.error('Failed to load incident details:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDetail();
    const interval = setInterval(fetchDetail, 10000); // 10s auto-refresh
    return () => clearInterval(interval);
  }, [incidentId]);

  const handleApprove = async (actionId: string, reason?: string) => {
    await approveAction(incidentId, actionId, 'approved', reason);
    await fetchDetail();
  };

  const handleDeny = async (actionId: string, reason?: string) => {
    await approveAction(incidentId, actionId, 'denied', reason);
    await fetchDetail();
  };

  const handleSteerAction = async (actionId: string, instruction: string) => {
    await approveAction(incidentId, actionId, 'steer', undefined, instruction);
    await fetchDetail();
  };

  const handlePause = async (reason: string) => {
    await pauseIncident(incidentId, reason);
    await fetchDetail();
  };

  const handleResume = async (instruction?: string) => {
    await resumeIncident(incidentId, instruction);
    await fetchDetail();
  };

  const handleEscalate = async (reason: string) => {
    await escalateIncident(incidentId, reason);
    await fetchDetail();
  };

  const handleClose = async (reason: string, generateReport: boolean = false) => {
    await closeIncident(incidentId, reason, generateReport);
    await fetchDetail();
  };

  const handleDrop = async (reason: string) => {
    await dropIncident(incidentId, reason);
    await fetchDetail();
  };

  const handleSteer = async (instruction: string) => {
    await steerIncident(incidentId, instruction);
    await fetchDetail();
  };

  const handleDenyRemediation = async (reason: string) => {
    if (actionRecord?.action_id) {
      await approveAction(incidentId, actionRecord.action_id, 'denied', reason);
    } else {
      try {
        const act = await getIncidentAction(incidentId);
        if (act?.action_id) {
          await approveAction(incidentId, act.action_id, 'denied', reason);
        } else {
          await escalateIncident(incidentId, `Deny: ${reason}`);
        }
      } catch {
        await escalateIncident(incidentId, `Deny: ${reason}`);
      }
    }
    await fetchDetail();
  };

  if (loading && !incident) {
    return (
      <div style={{ padding: '40px', textAlign: 'center', color: '#94a3b8' }}>
        Loading incident {incidentId}...
      </div>
    );
  }

  if (!incident) {
    return (
      <div style={{ padding: '40px', textAlign: 'center', color: '#f87171' }}>
        Incident {incidentId} not found.
      </div>
    );
  }

  return (
    <div style={{ padding: '0 24px 32px 24px' }}>
      {/* Back and Title Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          <button onClick={onBack} className="btn btn-secondary btn-sm" title="Back to incidents list">
            <ArrowLeft size={14} />
            <span>Fleet Incidents</span>
          </button>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <h2 className="mono" style={{ fontSize: '1.25rem', fontWeight: 800, color: '#38bdf8' }}>
                {incident.incident_id}
              </h2>
              <span className={`badge ${incident.severity === 'P1' ? 'badge-p1' : incident.severity === 'P2' ? 'badge-p2' : 'badge-p3'}`}>
                {incident.severity}
              </span>
              <span className="badge badge-state">
                {incident.state}
              </span>
            </div>
            <p style={{ fontSize: '0.8rem', color: '#94a3b8', marginTop: '2px' }}>
              Service: <strong style={{ color: '#f8fafc' }}>{incident.affected_service}</strong> | Namespace:{' '}
              <span className="mono">{incident.affected_namespace || 'default'}</span>
            </p>
          </div>
        </div>

        <button onClick={fetchDetail} className="btn btn-ghost btn-sm" title="Refresh details">
          <RefreshCw size={14} />
          <span>Refresh</span>
        </button>
      </div>

      {/* Action Controls (Standby, Resume, Escalate, Close, Steer, Deny) */}
      <ActionControls
        incident={incident}
        onPause={handlePause}
        onResume={handleResume}
        onEscalate={handleEscalate}
        onClose={handleClose}
        onSteer={handleSteer}
        onDeny={handleDenyRemediation}
        onDrop={handleDrop}
        loading={isEmergencyHalted}
      />

      {/* Visual State Progression Stepper */}
      <StateIndicator
        currentState={incident.state}
        retryCount={incident.retry_count}
        visitedStates={incident.visited_states}
        lastTransition={incident.last_transition}
      />

      {/* SAG Approval Queue Banner (if proposal exists or state is awaiting approval) */}
      {(incident.state === 'AWAITING_APPROVAL' || actionRecord) && (
        <ApprovalQueue
          actionRecord={actionRecord}
          incidentState={incident.state}
          onApprove={handleApprove}
          onDeny={handleDeny}
          onSteer={handleSteerAction}
          loading={isEmergencyHalted}
        />
      )}

      {/* View Tabs */}
      <div style={{ display: 'flex', gap: '8px', marginBottom: '16px', borderBottom: '1px solid var(--border-subtle)', paddingBottom: '8px' }}>
        <button
          onClick={() => setActiveTab('overview')}
          className={`btn btn-sm ${activeTab === 'overview' ? 'btn-primary' : 'btn-ghost'}`}
        >
          Overview & Telemetry
        </button>
        <button
          onClick={() => setActiveTab('ledger')}
          className={`btn btn-sm ${activeTab === 'ledger' ? 'btn-primary' : 'btn-ghost'}`}
        >
          Audit Ledger ({ledger.length})
        </button>
        <button
          onClick={() => setActiveTab('topology')}
          className={`btn btn-sm ${activeTab === 'topology' ? 'btn-primary' : 'btn-ghost'}`}
        >
          Service Topology
        </button>
        {reportData?.exists && (
          <button
            onClick={() => setActiveTab('report')}
            className={`btn btn-sm ${activeTab === 'report' ? 'btn-primary' : 'btn-ghost'}`}
            style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
          >
            <span>📄 Postmortem Report</span>
            <span
              style={{
                backgroundColor: 'rgba(16, 185, 129, 0.2)',
                color: '#34d399',
                fontSize: '0.65rem',
                fontWeight: 800,
                padding: '1px 5px',
                borderRadius: '4px',
                letterSpacing: '0.04em',
              }}
            >
              READY
            </span>
          </button>
        )}
      </div>

      {/* Tab Panels */}
      {activeTab === 'overview' && (
        <div>
          <FindingsSummary incident={incident} />
        </div>
      )}

      {activeTab === 'ledger' && (
        <LedgerTimeline entries={ledger} />
      )}

      {activeTab === 'topology' && (
        <TopologyDiagram
          graph={topology}
          affectedService={incident.affected_service}
          initialNamespace={incident.affected_namespace}
          hideNamespaceDropdown={true}
        />
      )}

      {activeTab === 'report' && reportData?.content && (
        <IncidentReportViewer
          incidentId={incident.incident_id}
          markdownContent={reportData.content}
        />
      )}
    </div>
  );
};
