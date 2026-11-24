import React from 'react';
import { Incident } from '../types';
import { Activity, Compass } from './Icons';

interface FindingsSummaryProps {
  incident: Incident;
}

export const FindingsSummary: React.FC<FindingsSummaryProps> = ({ incident }) => {
  const context = incident.context || {};
  const hypothesis = context.primary_hypothesis || 'Phase 1 multi-agent investigation in progress...';
  const confidence = context.confidence !== undefined ? Math.round(context.confidence * 100) : null;
  const corroboration = context.corroboration_score !== undefined ? context.corroboration_score : null;
  const evidence = context.ranked_evidence || [];
  const latestSteering = context.latest_steering || null;

  return (
    <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px', marginBottom: '20px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Activity size={18} color="#38bdf8" />
          <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#f8fafc' }}>Investigation Findings</h3>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          {confidence !== null && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>Confidence:</span>
              <div style={{
                width: '80px',
                height: '8px',
                backgroundColor: 'rgba(148, 163, 184, 0.2)',
                borderRadius: '4px',
                overflow: 'hidden',
              }}>
                <div style={{
                  width: `${confidence}%`,
                  height: '100%',
                  backgroundColor: confidence > 80 ? '#10b981' : confidence > 50 ? '#f59e0b' : '#f43f5e',
                }} />
              </div>
              <span className="mono" style={{ fontSize: '0.75rem', fontWeight: 700, color: '#f8fafc' }}>
                {confidence}%
              </span>
            </div>
          )}

          {corroboration !== null && (
            <span style={{ fontSize: '0.75rem', color: '#cbd5e1' }}>
              Corroboration Score: <strong className="mono" style={{ color: '#38bdf8' }}>{corroboration}</strong>
            </span>
          )}
        </div>
      </div>

      {/* Primary Hypothesis Box */}
      <div style={{
        padding: '14px 18px',
        borderRadius: '8px',
        backgroundColor: 'rgba(56, 189, 248, 0.06)',
        border: '1px solid rgba(56, 189, 248, 0.2)',
        marginBottom: '16px',
      }}>
        <div style={{ fontSize: '0.72rem', fontWeight: 700, color: '#38bdf8', textTransform: 'uppercase', marginBottom: '4px' }}>
          Primary Suspected Root Cause
        </div>
        <p style={{ fontSize: '0.9rem', color: '#f1f5f9', lineHeight: 1.5 }}>
          {hypothesis}
        </p>
      </div>

      {/* Active Steering Guidance Box (if present) */}
      {latestSteering && (
        <div style={{
          padding: '12px 16px',
          borderRadius: '8px',
          backgroundColor: 'rgba(168, 85, 247, 0.08)',
          border: '1px solid rgba(168, 85, 247, 0.25)',
          marginBottom: '16px',
          display: 'flex',
          alignItems: 'flex-start',
          gap: '10px',
        }}>
          <Compass size={18} color="#c084fc" style={{ marginTop: '2px', flexShrink: 0 }} />
          <div>
            <span style={{ fontSize: '0.72rem', fontWeight: 700, color: '#c084fc', textTransform: 'uppercase' }}>
              Active Operator Steering Guidance
            </span>
            <p style={{ fontSize: '0.85rem', color: '#e9d5ff', marginTop: '2px' }}>
              "{latestSteering}"
            </p>
          </div>
        </div>
      )}

      {/* Telemetry Evidence Breakdown */}
      {evidence.length > 0 && (
        <div>
          <h4 style={{ fontSize: '0.8rem', fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', marginBottom: '10px' }}>
            Supporting Telemetry Signals ({evidence.length})
          </h4>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {evidence.map((ev, idx) => (
              <div key={idx} className="glass-card" style={{ padding: '10px 14px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <span className="badge badge-state" style={{ fontSize: '0.675rem' }}>
                    {ev.signal_type}
                  </span>
                  <span style={{ fontSize: '0.825rem', color: '#f8fafc' }}>
                    {ev.description}
                  </span>
                </div>
                {ev.peak_value && (
                  <span className="mono" style={{ fontSize: '0.75rem', color: '#f59e0b' }}>
                    Peak: {String(ev.peak_value)}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
};
