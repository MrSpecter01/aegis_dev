import React from 'react';
import { IncidentState } from '../types';
import { CheckCircle, AlertTriangle, Pause } from './Icons';

interface StateIndicatorProps {
  currentState: IncidentState;
  retryCount?: number;
  visitedStates?: IncidentState[];
  lastTransition?: { from: IncidentState; to: IncidentState; timestamp?: string };
}

const MAIN_PIPELINE: IncidentState[] = [
  'NEW',
  'TRIAGE',
  'MERGING',
  'REMEDIATION',
  'AWAITING_APPROVAL',
  'EXECUTING',
  'MONITORING',
  'REPORTING',
  'CLOSED',
];

export const StateIndicator: React.FC<StateIndicatorProps> = ({
  currentState,
  retryCount = 0,
  visitedStates,
  lastTransition,
}) => {
  const isBranchState = currentState === 'STANDBY' || currentState === 'ESCALATED';

  return (
    <div className="glass-panel" style={{ padding: '20px 24px', marginBottom: '20px', borderRadius: '12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <span style={{ fontSize: '0.85rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', color: '#94a3b8' }}>
            State Machine Progression
          </span>
          <span className="badge badge-state-active" style={{ fontSize: '0.75rem' }}>
            {currentState}
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          {visitedStates && visitedStates.length > 0 && (
            <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
              Visited <strong style={{ color: '#f8fafc' }}>{visitedStates.length}</strong> lifecycle states
            </span>
          )}
          {retryCount > 0 && (
            <span style={{ fontSize: '0.75rem', color: '#f59e0b', fontWeight: 600 }}>
              Remediation Retry Cycle: #{retryCount}
            </span>
          )}
        </div>
      </div>

      {/* Main Sequential Flow */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        position: 'relative',
        overflowX: 'auto',
        padding: '8px 0',
      }}>
        {MAIN_PIPELINE.map((st, idx) => {
          const isCurrent = currentState === st;
          // State was actually visited if present in visitedStates list
          const isVisited = visitedStates && visitedStates.length > 0
            ? visitedStates.includes(st)
            : (st === 'NEW' || isCurrent);

          const isPassed = !isCurrent && isVisited;
          const isSkipped = !isCurrent && !isVisited;

          let nodeBg = 'rgba(15, 23, 42, 0.8)';
          let nodeBorder = 'var(--border-subtle)';
          let nodeColor = '#64748b';
          let glow = 'none';

          if (isCurrent) {
            nodeBg = 'rgba(56, 189, 248, 0.2)';
            nodeBorder = '#38bdf8';
            nodeColor = '#38bdf8';
            glow = '0 0 12px rgba(56, 189, 248, 0.5)';
            if (st === 'AWAITING_APPROVAL') {
              nodeBg = 'rgba(245, 158, 11, 0.2)';
              nodeBorder = '#f59e0b';
              nodeColor = '#f59e0b';
              glow = '0 0 14px rgba(245, 158, 11, 0.6)';
            }
          } else if (isPassed) {
            nodeBg = 'rgba(16, 185, 129, 0.15)';
            nodeBorder = '#10b981';
            nodeColor = '#10b981';
          } else if (isSkipped) {
            nodeBg = 'rgba(15, 23, 42, 0.3)';
            nodeBorder = 'rgba(51, 65, 85, 0.5)';
            nodeColor = '#475569';
          }

          return (
            <React.Fragment key={st}>
              {/* State Node */}
              <div style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: '6px',
                zIndex: 2,
                minWidth: '70px',
                opacity: isSkipped ? 0.6 : 1,
              }}>
                <div style={{
                  width: '28px',
                  height: '28px',
                  borderRadius: '50%',
                  backgroundColor: nodeBg,
                  border: isSkipped ? '1.5px dashed rgba(71, 85, 105, 0.6)' : `2px solid ${nodeBorder}`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  boxShadow: glow,
                  transition: 'all 0.2s ease',
                }}
                title={isCurrent ? `Active State: ${st}` : isPassed ? `Visited & Completed: ${st}` : `Unvisited / Bypassed: ${st}`}
                >
                  {isPassed ? (
                    <CheckCircle size={14} color="#10b981" />
                  ) : isSkipped ? (
                    <span style={{ fontSize: '0.75rem', fontWeight: 700, color: '#475569' }}>
                      —
                    </span>
                  ) : (
                    <span style={{ fontSize: '0.65rem', fontWeight: 800, color: nodeColor }}>
                      {idx + 1}
                    </span>
                  )}
                </div>
                <span style={{
                  fontSize: '0.675rem',
                  fontWeight: isCurrent ? 700 : isPassed ? 600 : 400,
                  color: isCurrent ? '#f8fafc' : isPassed ? '#10b981' : '#64748b',
                  textAlign: 'center',
                  whiteSpace: 'nowrap',
                }}>
                  {st.replace('_', ' ')}
                </span>
              </div>

              {/* Connecting line */}
              {idx < MAIN_PIPELINE.length - 1 && (
                <div style={{
                  flex: 1,
                  height: '2px',
                  backgroundColor: isPassed ? '#10b981' : 'var(--border-subtle)',
                  minWidth: '16px',
                  marginTop: '-16px',
                  zIndex: 1,
                }} />
              )}
            </React.Fragment>
          );
        })}
      </div>

      {/* Latest Direct Transition Indicator */}
      {lastTransition && lastTransition.from !== lastTransition.to && (
        <div style={{
          marginTop: '14px',
          padding: '8px 14px',
          borderRadius: '6px',
          backgroundColor: 'rgba(15, 23, 42, 0.6)',
          border: '1px solid var(--border-subtle)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          fontSize: '0.75rem',
        }}>
          <span style={{ color: '#94a3b8' }}>
            Latest Transition: <strong style={{ color: '#e2e8f0' }}>{lastTransition.from}</strong> ➔ <strong style={{ color: currentState === 'CLOSED' ? '#10b981' : currentState === 'ESCALATED' ? '#f43f5e' : '#38bdf8' }}>{lastTransition.to}</strong>
          </span>
          <span style={{ color: '#64748b', fontSize: '0.7rem' }}>
            {lastTransition.timestamp ? new Date(lastTransition.timestamp).toLocaleTimeString() : ''}
          </span>
        </div>
      )}

      {/* Branch Indicators (STANDBY / ESCALATED) */}
      {isBranchState && (
        <div style={{
          marginTop: '16px',
          padding: '10px 16px',
          borderRadius: '8px',
          backgroundColor: currentState === 'STANDBY' ? 'rgba(168, 85, 247, 0.12)' : 'rgba(244, 63, 94, 0.12)',
          border: `1px solid ${currentState === 'STANDBY' ? 'rgba(168, 85, 247, 0.3)' : 'rgba(244, 63, 94, 0.3)'}`,
          display: 'flex',
          alignItems: 'center',
          gap: '10px',
        }}>
          {currentState === 'STANDBY' ? (
            <>
              <Pause size={18} color="#c084fc" />
              <div>
                <span style={{ fontWeight: 700, color: '#e9d5ff', fontSize: '0.825rem' }}>INCIDENT IN STANDBY</span>
                <p style={{ fontSize: '0.75rem', color: '#cbd5e1' }}>Automated operations are paused. The incident is awaiting operator guidance or resume.</p>
              </div>
            </>
          ) : (
            <>
              <AlertTriangle size={18} color="#f43f5e" />
              <div>
                <span style={{ fontWeight: 700, color: '#fca5a5', fontSize: '0.825rem' }}>INCIDENT ESCALATED</span>
                <p style={{ fontSize: '0.75rem', color: '#cbd5e1' }}>Automated remediation was halted or denied. On-call human intervention required.</p>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
};
