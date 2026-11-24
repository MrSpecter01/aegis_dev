import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { Incident } from '../types';
import { AlertTriangle, Pause, Play, CheckCircle, Compass, XCircle, StopCircle } from './Icons';

interface ActionControlsProps {
  incident: Incident;
  onPause: (reason: string) => Promise<void>;
  onResume: (instruction?: string) => Promise<void>;
  onEscalate: (reason: string) => Promise<void>;
  onClose: (reason: string, generateReport?: boolean) => Promise<void>;
  onSteer: (instruction: string) => Promise<void>;
  onDeny?: (reason: string) => Promise<void>;
  onDrop?: (reason: string) => Promise<void>;
  loading?: boolean;
}

export const ActionControls: React.FC<ActionControlsProps> = ({
  incident,
  onPause,
  onResume,
  onEscalate,
  onClose,
  onSteer,
  onDeny,
  onDrop,
  loading,
}) => {
  const [modalAction, setModalAction] = useState<'pause' | 'resume' | 'escalate' | 'close' | 'steer' | 'deny' | 'drop' | null>(null);
  const [inputText, setInputText] = useState('');
  const [generateReport, setGenerateReport] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const isClosed = incident.state === 'CLOSED' || incident.state === 'RESOLVED';
  if (isClosed) return null;

  const isStandby = incident.state === 'STANDBY';
  const canPause = [
    'NEW',
    'GROUPING',
    'TRIAGE',
    'MERGING',
    'REMEDIATION',
    'AWAITING_APPROVAL',
    'EXECUTING',
    'MONITORING',
  ].includes(incident.state);
  const canResume = isStandby;
  const canSteer = ['AWAITING_APPROVAL', 'ESCALATED', 'REMEDIATION', 'STANDBY'].includes(incident.state);
  const canEscalate = incident.state !== 'ESCALATED' && incident.state !== 'REPORTING';
  const canDeny = Boolean(onDeny) && (incident.state === 'AWAITING_APPROVAL' || incident.state === 'EXECUTING');

  const handleAction = async () => {
    if (!modalAction) return;
    setSubmitting(true);
    try {
      if (modalAction === 'pause') {
        await onPause(inputText || 'Operator requested incident standby');
      } else if (modalAction === 'resume') {
        await onResume(inputText || undefined);
      } else if (modalAction === 'escalate') {
        await onEscalate(inputText || 'Operator force escalated');
      } else if (modalAction === 'close') {
        await onClose(inputText || (generateReport ? 'Operator closed with report' : 'Operator manual close'), generateReport);
      } else if (modalAction === 'steer') {
        await onSteer(inputText);
      } else if (modalAction === 'deny' && onDeny) {
        await onDeny(inputText || 'Operator invoked Deny');
      } else if (modalAction === 'drop' && onDrop) {
        await onDrop(inputText || 'Operator dropped incident');
      }
      setModalAction(null);
      setInputText('');
      setGenerateReport(false);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="glass-panel" style={{
      padding: '16px 20px',
      borderRadius: '12px',
      marginBottom: '20px',
      display: 'flex',
      flexWrap: 'wrap',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: '12px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span style={{ fontSize: '0.8rem', fontWeight: 700, textTransform: 'uppercase', color: '#94a3b8' }}>
          Operator Controls
        </span>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '10px' }}>
        {/* Standby / Resume */}
        {canResume ? (
          <button
            onClick={() => setModalAction('resume')}
            className="btn btn-sm btn-success"
            disabled={loading}
            title="Resume incident operations"
          >
            <Play size={14} />
            <span>Resume Operations</span>
          </button>
        ) : canPause ? (
          <button
            onClick={() => setModalAction('pause')}
            className="btn btn-sm btn-secondary"
            disabled={loading}
            title="Pause incident operations and set in Standby"
          >
            <Pause size={14} />
            <span>Standby / Pause</span>
          </button>
        ) : null}

        {/* Steer */}
        {canSteer && (
          <button
            onClick={() => setModalAction('steer')}
            className="btn btn-sm btn-secondary"
            disabled={loading}
            title="Inject guidance to steer remediation"
          >
            <Compass size={14} color="#c084fc" />
            <span>Steer Agent</span>
          </button>
        )}

        {/* Force Escalate */}
        {canEscalate && (
          <button
            onClick={() => setModalAction('escalate')}
            className="btn btn-sm btn-warning"
            disabled={loading}
            title="Force escalate incident to on-call bridge"
          >
            <AlertTriangle size={14} />
            <span>Force Escalate</span>
          </button>
        )}

        {/* Deny Remediation / Actions */}
        {canDeny && (
          <button
            onClick={() => setModalAction('deny')}
            className="btn btn-sm"
            style={{
              backgroundColor: 'rgba(239, 68, 68, 0.2)',
              border: '1px solid rgba(239, 68, 68, 0.5)',
              color: '#fca5a5',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
            disabled={loading}
            title="Deny actions and escalate incident"
          >
            <XCircle size={14} color="#ef4444" />
            <span>Deny</span>
          </button>
        )}

        {/* Drop Issue */}
        {onDrop && (
          <button
            onClick={() => setModalAction('drop')}
            className="btn btn-sm"
            style={{
              backgroundColor: 'rgba(244, 63, 94, 0.15)',
              border: '1px solid rgba(244, 63, 94, 0.4)',
              color: '#fda4af',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
            disabled={loading}
            title="Drop issue: halt system and agents from further work on this incident and close it"
          >
            <StopCircle size={14} color="#f43f5e" />
            <span>Drop Issue</span>
          </button>
        )}

        {/* Manual Close */}
        <button
          onClick={() => setModalAction('close')}
          className="btn btn-sm btn-danger"
          disabled={loading}
          title="Manually close this incident"
        >
          <CheckCircle size={14} />
          <span>Manual Close</span>
        </button>
      </div>

      {/* Action Modal */}
      {modalAction && createPortal(
        <div
          onClick={(e) => {
            if (e.target === e.currentTarget && !submitting) {
              setModalAction(null);
              setInputText('');
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
          <div className="glass-panel" style={{ width: '100%', maxWidth: '440px', padding: '24px' }}>
            <h3 style={{ fontSize: '1.05rem', fontWeight: 700, color: '#f8fafc', marginBottom: '8px' }}>
              {modalAction === 'pause' && 'Pause Incident Operations'}
              {modalAction === 'resume' && 'Resume Incident Operations'}
              {modalAction === 'escalate' && 'Force Escalate Incident'}
              {modalAction === 'close' && 'Manual Close Incident'}
              {modalAction === 'steer' && 'Provide Remediation Guidance'}
              {modalAction === 'deny' && 'Deny Remediation Action'}
              {modalAction === 'drop' && 'Drop Incident — Halt Agent Work'}
            </h3>

            <p style={{ fontSize: '0.8rem', color: '#cbd5e1', marginBottom: '14px', lineHeight: 1.4 }}>
              {modalAction === 'pause' && 'This will transition the incident to STANDBY. All autonomous agent loops and action dispatches will be suspended.'}
              {modalAction === 'resume' && 'This will resume operations. You may optionally include steering instructions to guide the next remediation attempt.'}
              {modalAction === 'escalate' && 'This will immediately transition the incident to ESCALATED and notify on-call engineers.'}
              {modalAction === 'close' && 'This will finalize and close the incident, cleanup fingerprint deduplication, and dump the ledger.'}
              {modalAction === 'steer' && 'The agent will read your instructions and immediately re-plan using the guidance.'}
              {modalAction === 'deny' && 'A denial immediately blocks remediation write-tools and transitions the incident to ESCALATED.'}
              {modalAction === 'drop' && 'Dropping this incident permanently halts system and agent execution, cancels pending actions, and sets the incident state to CLOSED.'}
            </p>

            <div style={{ marginBottom: '18px' }}>
              <label style={{ display: 'block', fontSize: '0.75rem', fontWeight: 600, color: '#94a3b8', marginBottom: '6px' }}>
                {modalAction === 'steer' ? 'Steering Prompt *' : modalAction === 'resume' ? 'Optional Steering Instructions' : modalAction === 'deny' ? 'Reason for Denial (Optional)' : modalAction === 'drop' ? 'Reason for Dropping (Optional)' : 'Reason / Note *'}
              </label>
              <textarea
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                placeholder={
                  modalAction === 'steer'
                    ? 'Enter specific instructions (e.g., Do not restart the database, only scale backend)...'
                    : modalAction === 'resume'
                    ? 'Optional guidance for next phase...'
                    : modalAction === 'deny'
                    ? 'Reason for rejecting this action proposal (e.g. Risk too high, freeze period)...'
                    : modalAction === 'drop'
                    ? 'Reason for dropping this issue (e.g. False positive, duplicate, external fix)...'
                    : 'Provide context for this action...'
                }
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
                }}
              />
            </div>

            {modalAction === 'close' && (
              <div style={{
                marginBottom: '16px',
                padding: '10px 14px',
                backgroundColor: 'rgba(59, 130, 246, 0.1)',
                border: '1px solid rgba(59, 130, 246, 0.3)',
                borderRadius: '8px',
              }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer', fontSize: '0.82rem', color: '#e2e8f0' }}>
                  <input
                    type="checkbox"
                    checked={generateReport}
                    onChange={(e) => setGenerateReport(e.target.checked)}
                    style={{ accentColor: '#3b82f6', width: '16px', height: '16px', cursor: 'pointer' }}
                  />
                  <span><strong>Generate postmortem report & delivery</strong> before closing (Phase 3 Scribe)</span>
                </label>
              </div>
            )}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                onClick={() => { setModalAction(null); setInputText(''); setGenerateReport(false); }}
                className="btn btn-ghost btn-sm"
                disabled={submitting}
              >
                Cancel
              </button>
              <button
                onClick={handleAction}
                className={`btn btn-sm ${
                  modalAction === 'close' || modalAction === 'deny' || modalAction === 'drop' ? 'btn-danger' :
                  modalAction === 'escalate' ? 'btn-warning' :
                  modalAction === 'resume' ? 'btn-success' : 'btn-primary'
                }`}
                disabled={submitting || (modalAction === 'steer' && !inputText.trim())}
              >
                {submitting
                  ? 'Applying...'
                  : modalAction === 'deny'
                  ? 'Confirm Deny'
                  : modalAction === 'drop'
                  ? 'Confirm Drop'
                  : modalAction === 'close'
                  ? (generateReport ? 'Close & Generate Report' : 'Confirm Close')
                  : 'Confirm'}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
};
