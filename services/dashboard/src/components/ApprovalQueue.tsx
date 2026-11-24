import React, { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { ActionRecord, IncidentState } from '../types';
import { AlertTriangle, CheckCircle, XCircle, Compass, Clock } from './Icons';

interface ApprovalQueueProps {
  actionRecord: ActionRecord | null;
  incidentState?: IncidentState;
  onApprove: (actionId: string, reason?: string) => Promise<void>;
  onDeny: (actionId: string, reason?: string) => Promise<void>;
  onSteer: (actionId: string, instruction: string) => Promise<void>;
  loading?: boolean;
}

export const ApprovalQueue: React.FC<ApprovalQueueProps> = ({
  actionRecord,
  incidentState,
  onApprove,
  onDeny,
  onSteer,
  loading,
}) => {
  const [modalMode, setModalMode] = useState<'approve' | 'deny' | 'steer' | null>(null);
  const [reasonInput, setReasonInput] = useState('');
  const [actionLoading, setActionLoading] = useState(false);
  const [timeLeft, setTimeLeft] = useState<number | null>(null);

  useEffect(() => {
    if (!actionRecord?.expires_at || actionRecord.status !== 'pending') {
      setTimeLeft(null);
      return;
    }

    const updateTimer = () => {
      const exp = new Date(actionRecord.expires_at!).getTime();
      const now = Date.now();
      const diffSec = Math.max(0, Math.floor((exp - now) / 1000));
      setTimeLeft(diffSec);
    };

    updateTimer();
    const interval = setInterval(updateTimer, 1000);
    return () => clearInterval(interval);
  }, [actionRecord]);

  if (!actionRecord) return null;

  const { action_id, proposal, status, channel, decided_by, decided_by_meta, auth_key_id, decided_at, reason, instruction } = actionRecord;
  const isPending = status === 'pending';
  const isTerminalOrReporting =
    incidentState === 'CLOSED' || incidentState === 'RESOLVED' || incidentState === 'REPORTING';
  const canApprove = !isTerminalOrReporting && isPending && incidentState === 'AWAITING_APPROVAL';
  const canSteer =
    !isTerminalOrReporting &&
    isPending &&
    (incidentState === 'AWAITING_APPROVAL' || incidentState === 'ESCALATED');
  const canDeny =
    !isTerminalOrReporting &&
    ((isPending && incidentState === 'AWAITING_APPROVAL') ||
      (status === 'approved' && (incidentState === 'AWAITING_APPROVAL' || incidentState === 'EXECUTING')));

  const handleConfirm = async () => {
    if (!modalMode) return;
    setActionLoading(true);
    try {
      if (modalMode === 'approve') {
        await onApprove(action_id, reasonInput);
      } else if (modalMode === 'deny') {
        await onDeny(action_id, reasonInput || 'Denied by dashboard operator');
      } else if (modalMode === 'steer') {
        await onSteer(action_id, reasonInput);
      }
      setModalMode(null);
      setReasonInput('');
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <div className="glass-panel" style={{
      padding: '24px',
      borderRadius: '12px',
      marginBottom: '20px',
      border: isPending ? '1px solid rgba(245, 158, 11, 0.4)' : '1px solid var(--border-subtle)',
      boxShadow: isPending ? '0 0 24px -4px rgba(245, 158, 11, 0.25)' : undefined,
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <div style={{
            backgroundColor: isPending ? 'rgba(245, 158, 11, 0.2)' : 'rgba(56, 189, 248, 0.15)',
            padding: '6px',
            borderRadius: '8px',
          }}>
            <AlertTriangle size={18} color={isPending ? '#f59e0b' : '#38bdf8'} />
          </div>
          <div>
            <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#f8fafc' }}>
              Safety Action Gateway (SAG) Gate
            </h3>
            <span className="mono" style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
              Action ID: {action_id}
            </span>
          </div>
        </div>

        {/* Status Pill or Countdown */}
        {isPending && timeLeft !== null ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '4px 10px', backgroundColor: 'rgba(245, 158, 11, 0.15)', borderRadius: '999px', border: '1px solid rgba(245, 158, 11, 0.3)' }}>
            <Clock size={13} color="#f59e0b" />
            <span className="mono" style={{ fontSize: '0.75rem', fontWeight: 700, color: '#fde68a' }}>
              Timeout: {timeLeft}s
            </span>
          </div>
        ) : (
          <span className={`badge ${
            status === 'approved' ? 'badge-state-resolved' :
            status === 'denied' ? 'badge-state-escalated' :
            status === 'steered' ? 'badge-state-standby' : 'badge-state'
          }`}>
            Decided: {status}
          </span>
        )}
      </div>

      {/* External Channel Decision Banner (e.g. Telegram or Previous Decision) */}
      {!isPending && (
        <div style={{
          padding: '12px 16px',
          borderRadius: '8px',
          backgroundColor: status === 'denied' ? 'rgba(244, 63, 94, 0.1)' : 'rgba(16, 185, 129, 0.1)',
          border: `1px solid ${status === 'denied' ? 'rgba(244, 63, 94, 0.3)' : 'rgba(16, 185, 129, 0.3)'}`,
          marginBottom: '16px',
          fontSize: '0.825rem',
        }}>
          <strong>Decision Record:</strong> {status.toUpperCase()} via{' '}
          <span className="mono" style={{ color: '#38bdf8' }}>{channel || 'system'}</span> by{' '}
          <strong
            style={{ color: '#f8fafc' }}
            title={[
              decided_by_meta?.telegram_user_id ? `Telegram ID: ${decided_by_meta.telegram_user_id}` : '',
              (auth_key_id || decided_by_meta?.key_id) ? `Key: ${auth_key_id || decided_by_meta?.key_id}` : '',
            ].filter(Boolean).join(' • ') || undefined}
          >
            {decided_by || 'operator'}
          </strong>
          {decided_at && <span style={{ color: '#94a3b8' }}> at {new Date(decided_at).toLocaleTimeString()}</span>}
          {reason && <p style={{ marginTop: '4px', color: '#cbd5e1' }}>Reason: "{reason}"</p>}
          {instruction && <p style={{ marginTop: '4px', color: '#c084fc' }}>Steering: "{instruction}"</p>}
        </div>
      )}

      {/* Proposal Details */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px', marginBottom: '20px' }}>
        {/* Rationale & Actions List */}
        <div className="glass-card" style={{ padding: '16px' }}>
          <h4 style={{ fontSize: '0.8rem', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', marginBottom: '8px' }}>
            Remediation Proposal
          </h4>
          {(() => {
            const rawRat = proposal?.rationale || proposal?.actions?.[0]?.rationale || 'No rationale provided';
            const cleanRat = rawRat.split('\n\n[Runbook Reference]')[0].split('[Runbook Ref:')[0].trim();
            return (
              <p style={{ fontSize: '0.85rem', color: '#f1f5f9', lineHeight: 1.5, marginBottom: '12px', whiteSpace: 'pre-line' }}>
                {cleanRat}
              </p>
            );
          })()}

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '8px' }}>
            <span className="badge badge-state">
              Confidence: {Math.round((proposal?.confidence || 0) * 100)}%
            </span>
            <span className="badge badge-state">
              Reversible: {proposal?.reversible ? 'Yes' : 'No'}
            </span>
          </div>

          <h5 style={{ fontSize: '0.75rem', fontWeight: 600, color: '#94a3b8', marginTop: '12px', marginBottom: '6px' }}>
            Planned Operations ({proposal?.actions?.length || 0}):
          </h5>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {proposal?.actions?.map((act, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.8rem' }}>
                <span className="mono" style={{ color: '#f59e0b', fontWeight: 600 }}>• {act.tool_name}</span>
                {act.target_resource && <span style={{ color: '#94a3b8' }}>on {act.target_resource}</span>}
              </div>
            ))}
          </div>

          {(() => {
            const citations =
              (proposal?.runbook_citations && proposal.runbook_citations.length > 0
                ? proposal.runbook_citations
                : proposal?.actions?.[0]?.runbook_citations || actionRecord.runbook_citations) || [];
            if (citations.length === 0) return null;
            return (
              <div style={{ marginTop: '14px', paddingTop: '12px', borderTop: '1px solid var(--border-subtle)' }}>
                <h5 style={{ fontSize: '0.75rem', fontWeight: 700, color: '#38bdf8', textTransform: 'uppercase', marginBottom: '8px' }}>
                  Runbook Citations ({citations.length})
                </h5>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {citations.map((cit, idx) => {
                    const idTitle =
                      cit.runbook_id && cit.book_name && cit.runbook_id.toLowerCase() !== cit.book_name.toLowerCase()
                        ? `${cit.runbook_id}/${cit.book_name}`
                        : (cit.runbook_id || cit.book_name || 'Runbook');
                    const secBase = cit.section || 'General';
                    const secLoc = (cit.line_range ? `${secBase}/${cit.line_range}` : secBase) + (cit.chunk_id ? ` (${cit.chunk_id})` : '');
                    return (
                      <div
                        key={idx}
                        style={{
                          padding: '10px 12px',
                          borderRadius: '6px',
                          backgroundColor: 'rgba(15, 23, 42, 0.7)',
                          border: '1px solid rgba(56, 189, 248, 0.25)',
                          fontSize: '0.78rem',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: '4px',
                        }}
                      >
                        <div style={{ color: '#f8fafc' }}>
                          <span className="mono" style={{ color: '#94a3b8' }}>id/title: </span>
                          <span className="mono" style={{ color: '#f8fafc', fontWeight: 600 }}>'{idTitle}'</span>
                        </div>
                        <div style={{ color: '#cbd5e1' }}>
                          <span className="mono" style={{ color: '#94a3b8' }}>section/line_range: </span>
                          <span className="mono" style={{ color: '#38bdf8' }}>'{secLoc}'</span>
                        </div>
                        {cit.referenced_text && (
                          <div style={{ color: '#cbd5e1', lineHeight: 1.4 }}>
                            <span className="mono" style={{ color: '#94a3b8' }}>reference: </span>
                            <span style={{ fontStyle: 'italic' }}>"{cit.referenced_text}"</span>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}
        </div>

        {/* Raw Parameters Inspector */}
        <div className="glass-card" style={{ padding: '16px', display: 'flex', flexDirection: 'column' }}>
          <h4 style={{ fontSize: '0.8rem', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', marginBottom: '8px' }}>
            Tool Parameters Payload
          </h4>
          <pre style={{
            flex: 1,
            margin: 0,
            padding: '12px',
            backgroundColor: 'rgba(7, 9, 14, 0.8)',
            border: '1px solid var(--border-subtle)',
            borderRadius: '6px',
            fontSize: '0.75rem',
            color: '#bae6fd',
            overflow: 'auto',
            minHeight: '120px',
            maxHeight: '280px',
          }}>
            {JSON.stringify(proposal?.actions || {}, null, 2)}
          </pre>
        </div>
      </div>

      {/* Operator Action Buttons — gated strictly by incident state and action status */}
      {(canApprove || canSteer || canDeny) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', justifyContent: 'flex-end' }}>
          {canSteer && (
            <button
              onClick={() => setModalMode('steer')}
              className="btn btn-secondary"
              disabled={loading}
            >
              <Compass size={15} color="#c084fc" />
              <span>Steer / Guide Fix</span>
            </button>
          )}

          {canDeny && (
            <button
              onClick={() => setModalMode('deny')}
              className="btn btn-danger"
              disabled={loading}
              title={status === 'approved' ? 'Deny previously approved action' : 'Deny action'}
            >
              <XCircle size={15} />
              <span>Deny</span>
            </button>
          )}

          {canApprove && (
            <button
              onClick={() => setModalMode('approve')}
              className="btn btn-success"
              disabled={loading}
              title="Approve & Execute"
            >
              <CheckCircle size={15} />
              <span>Approve & Execute</span>
            </button>
          )}
        </div>
      )}

      {/* Confirmation & Input Modal */}
      {modalMode && createPortal(
        <div
          onClick={(e) => {
            if (e.target === e.currentTarget && !actionLoading) {
              setModalMode(null);
              setReasonInput('');
            }
          }}
          style={{
            position: 'fixed',
            inset: 0,
            backgroundColor: 'rgba(7, 9, 14, 0.8)',
            backdropFilter: 'blur(8px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 99999,
            padding: '20px',
          }}
        >
          <div className="glass-panel" style={{ width: '100%', maxWidth: '480px', padding: '24px' }}>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 700, color: '#f8fafc', marginBottom: '8px' }}>
              {modalMode === 'approve' && 'Confirm Action Approval'}
              {modalMode === 'deny' && 'Confirm Action Denial'}
              {modalMode === 'steer' && 'Provide Human Steering Guidance'}
            </h3>

            <p style={{ fontSize: '0.825rem', color: '#cbd5e1', marginBottom: '16px', lineHeight: 1.5 }}>
              {modalMode === 'approve' && 'This will grant the SAG guard permission to execute the proposed mutating actions on the cluster.'}
              {modalMode === 'deny' && 'This action proposal will be marked as denied and the incident will be escalated.'}
              {modalMode === 'steer' && 'Provide natural language guidance to the remediation agent. The agent will re-plan taking your instructions into account.'}
            </p>

            <div style={{ marginBottom: '20px' }}>
              <label style={{ display: 'block', fontSize: '0.75rem', fontWeight: 600, color: '#94a3b8', marginBottom: '6px' }}>
                {modalMode === 'steer' ? 'Operator Steering Instructions *' : 'Optional Reason / Audit Notes'}
              </label>
              <textarea
                value={reasonInput}
                onChange={(e) => setReasonInput(e.target.value)}
                placeholder={modalMode === 'steer' ? 'e.g. Do not restart pod; rollback release to v1.3' : 'Enter audit note...'}
                rows={3}
                style={{
                  width: '100%',
                  padding: '10px',
                  backgroundColor: 'rgba(15, 23, 42, 0.8)',
                  border: '1px solid var(--border-medium)',
                  borderRadius: '6px',
                  color: '#ffffff',
                  fontSize: '0.85rem',
                  outline: 'none',
                  resize: 'none',
                }}
              />
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                onClick={() => { setModalMode(null); setReasonInput(''); }}
                className="btn btn-ghost"
                disabled={actionLoading}
              >
                Cancel
              </button>
              <button
                onClick={handleConfirm}
                className={`btn ${modalMode === 'approve' ? 'btn-success' : modalMode === 'deny' ? 'btn-danger' : 'btn-primary'}`}
                disabled={actionLoading || (modalMode === 'steer' && !reasonInput.trim())}
              >
                {actionLoading ? 'Submitting...' : 'Confirm'}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
};
