import React, { useState } from 'react';
import { Shield, AlertTriangle } from './Icons';
import { setApiKey, setStoredUser, getCurrentUser } from '../api/client';
import { AuthPrincipal } from '../types';

interface AuthModalProps {
  isOpen: boolean;
  onSuccess: (user: AuthPrincipal) => void;
  reason?: string;
}

export const AuthModal: React.FC<AuthModalProps> = ({ isOpen, onSuccess, reason }) => {
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKeyInput.trim()) {
      setError('Please provide an API Key');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      setApiKey(apiKeyInput.trim());
      const user = await getCurrentUser();
      setStoredUser(user);
      setApiKeyInput('');
      onSuccess(user);
    } catch (err: any) {
      setError(err.message || 'Invalid API Key. Authentication failed.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      backgroundColor: 'rgba(7, 9, 14, 0.85)',
      backdropFilter: 'blur(12px)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      zIndex: 9999,
      padding: '20px',
    }}>
      <div className="glass-panel" style={{ width: '100%', maxWidth: '440px', padding: '32px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px' }}>
          <div style={{
            background: 'linear-gradient(135deg, rgba(56, 189, 248, 0.2), rgba(99, 102, 241, 0.2))',
            padding: '10px',
            borderRadius: '12px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}>
            <Shield size={28} color="#38bdf8" />
          </div>
          <div>
            <h2 style={{ fontSize: '1.25rem', fontWeight: 700, color: '#f8fafc' }}>Aegis Control Plane</h2>
            <p style={{ fontSize: '0.8rem', color: '#94a3b8' }}>SRE Operational Access</p>
          </div>
        </div>

        {reason && (
          <div style={{
            background: 'rgba(244, 63, 94, 0.1)',
            border: '1px solid rgba(244, 63, 94, 0.25)',
            borderRadius: '8px',
            padding: '10px 14px',
            marginBottom: '16px',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            fontSize: '0.8rem',
            color: '#fca5a5',
          }}>
            <AlertTriangle size={16} color="#f87171" />
            <span>{reason}</span>
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: '20px' }}>
            <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, color: '#cbd5e1', marginBottom: '8px' }}>
              X-API-Key Authentication
            </label>
            <div style={{ position: 'relative' }}>
              <input
                type="password"
                value={apiKeyInput}
                onChange={(e) => setApiKeyInput(e.target.value)}
                placeholder="Enter cluster API key..."
                autoFocus
                style={{
                  width: '100%',
                  padding: '12px 14px',
                  backgroundColor: 'rgba(15, 23, 42, 0.8)',
                  border: '1px solid var(--border-medium)',
                  borderRadius: '8px',
                  color: '#ffffff',
                  fontSize: '0.9rem',
                  fontFamily: 'var(--font-mono)',
                  outline: 'none',
                }}
              />
            </div>
            <p style={{ fontSize: '0.72rem', color: '#64748b', marginTop: '6px' }}>
              Enter your API key (default admin key from <code className="mono">AEGIS_API_KEY</code> or a personal key mapped in <code className="mono">config/identities.yaml</code>).
            </p>
          </div>

          {error && (
            <div style={{ color: '#f87171', fontSize: '0.8rem', marginBottom: '16px' }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            className="btn btn-primary"
            disabled={loading}
            style={{ width: '100%', padding: '12px' }}
          >
            {loading ? 'Authenticating...' : 'Connect to Control Plane'}
          </button>
        </form>
      </div>
    </div>
  );
};
