import React, { useState, useEffect, useCallback } from 'react';
import { AuthPrincipal, Incident, SystemStatus, TopologyGraph } from './types';
import {
  getIncidents,
  getSystemStatus,
  pauseSystem,
  resumeSystem,
  getTopology,
  getApiKey,
  getCurrentUser,
  getStoredUser,
  setStoredUser,
  onAuthRequired,
  triggerDemoIncident,
} from './api/client';
import { Header } from './components/Header';
import { IncidentList } from './components/IncidentList';
import { IncidentDetail } from './components/IncidentDetail';
import { TopologyDiagram } from './components/TopologyDiagram';
import { ConfigEditor } from './components/ConfigEditor';
import { AuthModal } from './components/AuthModal';

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<'fleet' | 'topology' | 'config'>('fleet');
  const [selectedIncidentId, setSelectedIncidentId] = useState<string | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [topology, setTopology] = useState<TopologyGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [authReason, setAuthReason] = useState<string | undefined>();
  const [currentUser, setCurrentUser] = useState<AuthPrincipal | null>(() => getStoredUser());
  const [emergencyModalOpen, setEmergencyModalOpen] = useState(false);
  const [emergencyReason, setEmergencyReason] = useState('');
  const [emergencySubmitting, setEmergencySubmitting] = useState(false);

  // Auth failure hook
  useEffect(() => {
    return onAuthRequired(() => {
      setCurrentUser(null);
      setAuthReason('Session expired or invalid API key. Please re-authenticate.');
      setAuthModalOpen(true);
    });
  }, []);

  // Check initial API key and hydrate authenticated operator identity
  useEffect(() => {
    if (!getApiKey()) {
      setAuthReason('Please enter your personal Operator API key to connect to the control plane.');
      setAuthModalOpen(true);
      return;
    }
    getCurrentUser()
      .then((user) => {
        setStoredUser(user);
        setCurrentUser(user);
      })
      .catch(() => {
        // 401/403 triggers onAuthRequired handler
      });
  }, []);

  const fetchData = useCallback(async () => {
    if (!getApiKey()) return;
    setLoading(true);
    try {
      const [incList, sysStat] = await Promise.all([
        getIncidents().catch(() => []),
        getSystemStatus().catch(() => null),
      ]);
      setIncidents(incList);
      setSystemStatus(sysStat);
    } catch (err: any) {
      console.error('Fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load
  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Load topology when switching to topology tab
  useEffect(() => {
    if (activeTab === 'topology' && !topology) {
      getTopology().then(setTopology).catch(console.error);
    }
  }, [activeTab, topology]);

  // 10s background auto-refresh interval
  useEffect(() => {
    const timer = setInterval(() => {
      fetchData();
    }, 10000);
    return () => clearInterval(timer);
  }, [fetchData]);

  const handleToggleEmergencyHalt = () => {
    if (systemStatus?.paused) {
      // Resume immediately
      resumeSystem(currentUser?.username)
        .then(() => fetchData())
        .catch((err) => alert(`Failed to resume: ${err.message}`));
    } else {
      // Open modal to enter reason
      setEmergencyModalOpen(true);
    }
  };

  const handleConfirmEmergencyPause = async () => {
    setEmergencySubmitting(true);
    try {
      await pauseSystem(emergencyReason || 'Emergency brake triggered from dashboard');
      setEmergencyModalOpen(false);
      setEmergencyReason('');
      await fetchData();
    } catch (err: any) {
      alert(`Failed to trigger emergency stop: ${err.message}`);
    } finally {
      setEmergencySubmitting(false);
    }
  };

  const handleTriggerDemo = async () => {
    try {
      const res = await triggerDemoIncident();
      await fetchData();
      if (res && res.incident_id) {
        setSelectedIncidentId(res.incident_id);
      }
    } catch (err: any) {
      alert(`Failed to seed demo incident: ${err.message}`);
    }
  };

  const activeCount = incidents.filter(
    (i) => i.state !== 'CLOSED' && i.state !== 'RESOLVED'
  ).length;

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* Top Navigation & Status Bar */}
      <Header
        activeTab={activeTab}
        onSelectTab={(tab) => {
          setActiveTab(tab);
          setSelectedIncidentId(null);
        }}
        systemStatus={systemStatus}
        onToggleEmergencyHalt={handleToggleEmergencyHalt}
        onOpenAuth={() => {
          setAuthReason('Switch active Operator API Key');
          setAuthModalOpen(true);
        }}
        currentUser={currentUser}
        activeCount={activeCount}
        onTriggerDemo={handleTriggerDemo}
      />

      {/* Top Emergency Status Banner */}
      {Boolean(systemStatus?.paused) && (
        <div style={{
          backgroundColor: 'rgba(225, 29, 72, 0.25)',
          borderBottom: '1px solid rgba(244, 63, 94, 0.5)',
          padding: '10px 24px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          color: '#fecdd3',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', fontSize: '0.85rem' }}>
            <span style={{ fontSize: '1.1rem' }}>🚨</span>
            <span>
              <strong>FLEET-WIDE EMERGENCY HALT ACTIVE:</strong> Automated stream workers and phase loops are suspended. Mutating actions, approvals, and transitions are locked.
            </span>
          </div>
          <button
            onClick={handleToggleEmergencyHalt}
            className="btn btn-sm btn-success"
            style={{ padding: '4px 12px', fontSize: '0.75rem' }}
          >
            Resume Operations
          </button>
        </div>
      )}

      {/* Main Content Area */}
      <main style={{ flex: 1 }}>
        {selectedIncidentId ? (
          <IncidentDetail
            incidentId={selectedIncidentId}
            onBack={() => setSelectedIncidentId(null)}
            isEmergencyHalted={Boolean(systemStatus?.paused)}
          />
        ) : activeTab === 'fleet' ? (
          <IncidentList
            incidents={incidents}
            loading={loading}
            onSelectIncident={(id) => setSelectedIncidentId(id)}
            onRefresh={fetchData}
          />
        ) : activeTab === 'topology' ? (
          <div style={{ padding: '0 24px 32px 24px' }}>
            <TopologyDiagram
              graph={topology}
              onNamespaceChange={(ns) => {
                getTopology(ns).then(setTopology).catch(console.error);
              }}
            />
          </div>
        ) : (
          <ConfigEditor />
        )}
      </main>

      {/* Global Emergency Brake Modal */}
      {emergencyModalOpen && (
        <div style={{
          position: 'fixed',
          inset: 0,
          backgroundColor: 'rgba(7, 9, 14, 0.85)',
          backdropFilter: 'blur(8px)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 10001,
          padding: '20px',
        }}>
          <div className="glass-panel" style={{ width: '100%', maxWidth: '440px', padding: '24px', border: '1px solid rgba(244, 63, 94, 0.4)' }}>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 700, color: '#fca5a5', marginBottom: '8px' }}>
              Confirm Fleet-Wide System Halt
            </h3>
            <p style={{ fontSize: '0.825rem', color: '#cbd5e1', marginBottom: '16px', lineHeight: 1.5 }}>
              This will place the entire Aegis system into a halt state. All active phase workers will pause, and all SAG write tools will fail-closed.
            </p>
            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', fontSize: '0.75rem', fontWeight: 600, color: '#94a3b8', marginBottom: '6px' }}>
                Halt Reason / Reference *
              </label>
              <textarea
                value={emergencyReason}
                onChange={(e) => setEmergencyReason(e.target.value)}
                placeholder="e.g. Unstable control plane network or major rollout in progress..."
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
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                onClick={() => setEmergencyModalOpen(false)}
                className="btn btn-ghost btn-sm"
                disabled={emergencySubmitting}
              >
                Cancel
              </button>
              <button
                onClick={handleConfirmEmergencyPause}
                className="btn btn-danger btn-sm"
                disabled={emergencySubmitting}
              >
                {emergencySubmitting ? 'Halting Fleet...' : 'Confirm System Halt'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Auth Modal */}
      <AuthModal
        isOpen={authModalOpen}
        onSuccess={(user) => {
          setCurrentUser(user);
          setAuthModalOpen(false);
          setAuthReason(undefined);
          fetchData();
        }}
        reason={authReason}
      />
    </div>
  );
};
