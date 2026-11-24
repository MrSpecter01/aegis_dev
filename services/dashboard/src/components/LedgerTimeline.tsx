import React, { useState } from 'react';
import { LedgerEntry } from '../types';
import { Clock } from './Icons';

interface LedgerTimelineProps {
  entries: LedgerEntry[];
  loading?: boolean;
}

export const LedgerTimeline: React.FC<LedgerTimelineProps> = ({ entries, loading }) => {
  const [expandedEntries, setExpandedEntries] = useState<Record<string, boolean>>({});

  const toggleExpand = (id: string) => {
    setExpandedEntries((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const formatTimestamp = (ts: string) => {
    try {
      const d = new Date(ts);
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    } catch {
      return ts;
    }
  };

  const getActorBadgeColor = (source: string) => {
    if (source.includes('operator')) return { bg: 'rgba(245, 158, 11, 0.2)', text: '#fde68a' };
    if (source.includes('sag')) return { bg: 'rgba(244, 63, 94, 0.2)', text: '#fca5a5' };
    if (source.includes('remediation')) return { bg: 'rgba(168, 85, 247, 0.2)', text: '#e9d5ff' };
    if (source.includes('investigator')) return { bg: 'rgba(56, 189, 248, 0.2)', text: '#bae6fd' };
    return { bg: 'rgba(148, 163, 184, 0.15)', text: '#cbd5e1' };
  };

  if (loading) {
    return (
      <div className="glass-panel" style={{ padding: '32px', textAlign: 'center', color: '#94a3b8' }}>
        Loading ledger timeline...
      </div>
    );
  }

  return (
    <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Clock size={16} color="#38bdf8" />
          <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#f8fafc' }}>Audit Ledger Timeline</h3>
        </div>
        <span style={{ fontSize: '0.75rem', color: '#64748b' }}>
          {entries.length} Immutable Events
        </span>
      </div>

      {entries.length === 0 ? (
        <p style={{ fontSize: '0.825rem', color: '#64748b', textAlign: 'center', padding: '24px' }}>
          No ledger entries recorded yet.
        </p>
      ) : (
        <div style={{ position: 'relative', paddingLeft: '20px' }}>
          {/* Vertical connecting line */}
          <div style={{
            position: 'absolute',
            left: '6px',
            top: '8px',
            bottom: '8px',
            width: '2px',
            backgroundColor: 'var(--border-medium)',
          }} />

          {entries.map((entry) => {
            const isExpanded = !!expandedEntries[entry.entry_id];
            const actorStyle = getActorBadgeColor(entry.source);
            const hasDetails = entry.details && Object.keys(entry.details).length > 0;

            return (
              <div key={entry.entry_id} style={{ position: 'relative', marginBottom: '18px' }}>
                {/* Node dot on line */}
                <div style={{
                  position: 'absolute',
                  left: '-18px',
                  top: '5px',
                  width: '10px',
                  height: '10px',
                  borderRadius: '50%',
                  backgroundColor: '#38bdf8',
                  border: '2px solid var(--bg-base)',
                  boxShadow: '0 0 6px rgba(56, 189, 248, 0.6)',
                }} />

                <div className="glass-card" style={{ padding: '12px 16px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span className="mono" style={{ fontSize: '0.75rem', fontWeight: 600, color: '#94a3b8' }}>
                        {formatTimestamp(entry.timestamp)}
                      </span>
                      <span style={{
                        backgroundColor: actorStyle.bg,
                        color: actorStyle.text,
                        padding: '2px 7px',
                        borderRadius: '4px',
                        fontSize: '0.7rem',
                        fontWeight: 600,
                      }}>
                        {entry.source}
                      </span>
                      <span className="badge badge-state" style={{ fontSize: '0.675rem' }}>
                        {entry.state}
                      </span>
                    </div>

                    <span className="mono" style={{ fontSize: '0.75rem', fontWeight: 600, color: '#38bdf8' }}>
                      {entry.event_type}
                    </span>
                  </div>

                  {hasDetails && (
                    <div style={{ marginTop: '8px' }}>
                      <button
                        onClick={() => toggleExpand(entry.entry_id)}
                        className="btn btn-ghost btn-sm"
                        style={{ padding: '2px 6px', fontSize: '0.7rem', color: '#94a3b8' }}
                      >
                        {isExpanded ? 'Hide Payload ▲' : 'View Payload ▼'}
                      </button>

                      {isExpanded && (
                        <pre style={{
                          marginTop: '8px',
                          padding: '10px',
                          backgroundColor: 'rgba(7, 9, 14, 0.9)',
                          border: '1px solid var(--border-subtle)',
                          borderRadius: '6px',
                          fontSize: '0.75rem',
                          color: '#bae6fd',
                          overflowX: 'auto',
                          maxHeight: '260px',
                        }}>
                          {JSON.stringify(entry.details, null, 2)}
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};
