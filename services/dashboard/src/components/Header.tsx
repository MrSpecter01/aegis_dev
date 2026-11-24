import { Shield, Pause, Play, Settings, Layers, Activity, AlertTriangle, Key } from './Icons';
import { AuthPrincipal, SystemStatus } from '../types';

interface HeaderProps {
  activeTab: 'fleet' | 'topology' | 'config';
  onSelectTab: (tab: 'fleet' | 'topology' | 'config') => void;
  systemStatus: SystemStatus | null;
  onToggleEmergencyHalt: () => void;
  onOpenAuth?: () => void;
  currentUser?: AuthPrincipal | null;
  activeCount: number;
  onTriggerDemo?: () => void;
}

export const Header: React.FC<HeaderProps> = ({
  activeTab,
  onSelectTab,
  systemStatus,
  onToggleEmergencyHalt,
  onOpenAuth,
  currentUser,
  activeCount,
  onTriggerDemo,
}) => {
  const isPaused = systemStatus?.paused ?? false;

  return (
    <header className="glass-panel" style={{
      margin: '16px 24px',
      padding: '12px 24px',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      borderRadius: '14px',
    }}>
      {/* Brand & Fleet Status */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '20px' }}>
        <div 
          onClick={() => onSelectTab('fleet')}
          style={{ display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer' }}
        >
          <div style={{
            background: 'linear-gradient(135deg, #0284c7, #6366f1)',
            padding: '7px',
            borderRadius: '9px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            boxShadow: '0 0 16px rgba(56, 189, 248, 0.4)',
          }}>
            <Shield size={20} color="#ffffff" />
          </div>
          <div>
            <span style={{ fontSize: '1.1rem', fontWeight: 800, letterSpacing: '0.04em', color: '#ffffff' }}>AEGIS</span>
            <span style={{ fontSize: '0.72rem', color: '#94a3b8', marginLeft: '8px', textTransform: 'uppercase', letterSpacing: '0.08em' }}>SRE Control Plane</span>
          </div>
        </div>

        {/* Navigation Tabs */}
        <nav style={{ display: 'flex', gap: '6px', marginLeft: '16px' }}>
          <button
            onClick={() => onSelectTab('fleet')}
            className={`btn btn-sm ${activeTab === 'fleet' ? 'btn-primary' : 'btn-ghost'}`}
          >
            <Activity size={14} />
            <span>Incidents</span>
            {activeCount > 0 && (
              <span style={{
                backgroundColor: 'rgba(244, 63, 94, 0.3)',
                color: '#fca5a5',
                padding: '1px 6px',
                borderRadius: '999px',
                fontSize: '0.675rem',
                fontWeight: 700,
              }}>
                {activeCount}
              </span>
            )}
          </button>

          <button
            onClick={() => onSelectTab('topology')}
            className={`btn btn-sm ${activeTab === 'topology' ? 'btn-primary' : 'btn-ghost'}`}
          >
            <Layers size={14} />
            <span>Topology</span>
          </button>

          <button
            onClick={() => onSelectTab('config')}
            className={`btn btn-sm ${activeTab === 'config' ? 'btn-primary' : 'btn-ghost'}`}
          >
            <Settings size={14} />
            <span>Config</span>
          </button>
        </nav>
      </div>

      {/* Emergency Control & Auth Actions */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
        {/* System Global Status Badge */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '4px 12px',
          borderRadius: '20px',
          backgroundColor: isPaused ? 'rgba(244, 63, 94, 0.15)' : 'rgba(16, 185, 129, 0.12)',
          border: `1px solid ${isPaused ? 'rgba(244, 63, 94, 0.4)' : 'rgba(16, 185, 129, 0.3)'}`,
        }}>
          <span style={{
            width: '8px',
            height: '8px',
            borderRadius: '50%',
            backgroundColor: isPaused ? '#f43f5e' : '#10b981',
            boxShadow: isPaused ? '0 0 8px #f43f5e' : '0 0 8px #10b981',
          }} />
          <span style={{
            fontSize: '0.75rem',
            fontWeight: 700,
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
            color: isPaused ? '#fca5a5' : '#a7f3d0',
          }}>
            {isPaused ? 'Fleet Halted' : 'Operational'}
          </span>
        </div>

        {/* Operator Identity Badge / Switch Key */}
        {onOpenAuth && (
          <button
            onClick={onOpenAuth}
            className="btn btn-sm btn-ghost"
            title={currentUser ? `Signed in as ${currentUser.display_name || currentUser.username} (${currentUser.role}). Click to switch API key.` : 'Authenticate with API key'}
            style={{
              border: '1px solid var(--border-subtle)',
              backgroundColor: 'rgba(15, 23, 42, 0.65)',
              color: '#e2e8f0',
              gap: '6px',
            }}
          >
            <Key size={13} color="#38bdf8" />
            <span className="mono" style={{ fontSize: '0.75rem', fontWeight: 700 }}>
              {currentUser?.username || 'Sign In'}
            </span>
            {currentUser?.role && (
              <span style={{
                fontSize: '0.65rem',
                padding: '1px 5px',
                borderRadius: '4px',
                backgroundColor: 'rgba(56, 189, 248, 0.18)',
                color: '#7dd3fc',
                textTransform: 'uppercase',
              }}>
                {currentUser.role}
              </span>
            )}
          </button>
        )}

        {/* Global Emergency System Halt Button */}
        <button
          onClick={onToggleEmergencyHalt}
          className={`btn btn-sm ${isPaused ? 'btn-success' : 'btn-danger'}`}
          title={isPaused ? 'Resume automated agent and remediation operations' : 'System Halt: Halt all automated write tools and phase workers'}
        >
          {isPaused ? <Play size={14} /> : <Pause size={14} />}
          <span>{isPaused ? 'Resume Fleet' : 'System Halt'}</span>
        </button>

        {/* Demo Incident Button */}
        {onTriggerDemo && (
          <button
            onClick={onTriggerDemo}
            className="btn btn-sm btn-ghost"
            title="Seed a realistic interactive incident in AWAITING_APPROVAL state with pending actions"
            style={{ color: '#f59e0b', border: '1px solid rgba(245, 158, 11, 0.4)' }}
          >
            <AlertTriangle size={14} color="#f59e0b" />
            <span>+ Demo Incident</span>
          </button>
        )}
      </div>
    </header>
  );
};
