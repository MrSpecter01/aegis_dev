import React, { useState, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { Incident } from '../types';
import { Search, RefreshCw, AlertTriangle, Calendar, ArrowUp, ArrowDown, ArrowUpDown, Filter, XCircle, StopCircle } from './Icons';
import { getIncidentAction, approveAction, escalateIncident, dropIncident, getIncidentReport } from '../api/client';
import { IncidentReportViewer } from './IncidentReportViewer';

interface IncidentListProps {
  incidents: Incident[];
  loading: boolean;
  onSelectIncident: (incidentId: string) => void;
  onRefresh: () => void;
}

type SortKey = 'id' | 'severity' | 'state' | 'service' | 'duration' | 'created';
type SortDirection = 'asc' | 'desc';
type DatePreset = 'ALL' | '15M' | '1H' | '24H' | '7D' | 'CUSTOM';

export const IncidentList: React.FC<IncidentListProps> = ({
  incidents,
  loading,
  onSelectIncident,
  onRefresh,
}) => {
  const [stateFilter, setStateFilter] = useState<string>('ALL');
  const [severityFilter, setSeverityFilter] = useState<string>('ALL');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [datePreset, setDatePreset] = useState<DatePreset>('ALL');
  const [customFrom, setCustomFrom] = useState<string>('');
  const [customTo, setCustomTo] = useState<string>('');

  // Quick Deny & Drop State
  const [denyTargetIncident, setDenyTargetIncident] = useState<string | null>(null);
  const [denyReason, setDenyReason] = useState<string>('');
  const [denySubmitting, setDenySubmitting] = useState<boolean>(false);

  const [dropTargetIncident, setDropTargetIncident] = useState<string | null>(null);
  const [dropReason, setDropReason] = useState<string>('');
  const [dropSubmitting, setDropSubmitting] = useState<boolean>(false);

  // Report Modal State
  const [reportModalIncidentId, setReportModalIncidentId] = useState<string | null>(null);
  const [reportModalContent, setReportModalContent] = useState<string | null>(null);
  const [reportModalLoading, setReportModalLoading] = useState<boolean>(false);

  const handleOpenReport = async (incidentId: string) => {
    setReportModalIncidentId(incidentId);
    setReportModalLoading(true);
    setReportModalContent(null);
    try {
      const rep = await getIncidentReport(incidentId);
      if (rep.exists && rep.content) {
        setReportModalContent(rep.content);
      } else {
        alert(`No postmortem report generated for incident '${incidentId}' yet.`);
        setReportModalIncidentId(null);
      }
    } catch (err: any) {
      alert(`Failed to load report: ${err.message}`);
      setReportModalIncidentId(null);
    } finally {
      setReportModalLoading(false);
    }
  };

  // Sorting state
  const [sortKey, setSortKey] = useState<SortKey>('created');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');

  // Toggle sorting by column
  const handleSortColumn = (key: SortKey) => {
    if (sortKey === key) {
      setSortDirection((prev) => (prev === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      // Default directions
      if (key === 'created' || key === 'duration' || key === 'severity') {
        setSortDirection('desc');
      } else {
        setSortDirection('asc');
      }
    }
  };

  // Preset mapping for quick sort dropdown
  const currentSortPresetValue = useMemo(() => {
    if (sortKey === 'created' && sortDirection === 'desc') return 'TIME_DESC';
    if (sortKey === 'created' && sortDirection === 'asc') return 'TIME_ASC';
    if (sortKey === 'duration' && sortDirection === 'desc') return 'DURATION_DESC';
    if (sortKey === 'duration' && sortDirection === 'asc') return 'DURATION_ASC';
    if (sortKey === 'id' && sortDirection === 'asc') return 'ALPHA_ASC';
    if (sortKey === 'id' && sortDirection === 'desc') return 'ALPHA_DESC';
    if (sortKey === 'severity' && sortDirection === 'desc') return 'SEV_DESC';
    if (sortKey === 'severity' && sortDirection === 'asc') return 'SEV_ASC';
    if (sortKey === 'service' && sortDirection === 'asc') return 'SERVICE_ASC';
    if (sortKey === 'service' && sortDirection === 'desc') return 'SERVICE_DESC';
    return 'CUSTOM';
  }, [sortKey, sortDirection]);

  const handleSortDropdownChange = (value: string) => {
    switch (value) {
      case 'TIME_DESC':
        setSortKey('created');
        setSortDirection('desc');
        break;
      case 'TIME_ASC':
        setSortKey('created');
        setSortDirection('asc');
        break;
      case 'DURATION_DESC':
        setSortKey('duration');
        setSortDirection('desc');
        break;
      case 'DURATION_ASC':
        setSortKey('duration');
        setSortDirection('asc');
        break;
      case 'ALPHA_ASC':
        setSortKey('id');
        setSortDirection('asc');
        break;
      case 'ALPHA_DESC':
        setSortKey('id');
        setSortDirection('desc');
        break;
      case 'SEV_DESC':
        setSortKey('severity');
        setSortDirection('desc');
        break;
      case 'SEV_ASC':
        setSortKey('severity');
        setSortDirection('asc');
        break;
      case 'SERVICE_ASC':
        setSortKey('service');
        setSortDirection('asc');
        break;
      case 'SERVICE_DESC':
        setSortKey('service');
        setSortDirection('desc');
        break;
    }
  };

  const isFiltered = useMemo(() => {
    return (
      stateFilter !== 'ALL' ||
      severityFilter !== 'ALL' ||
      searchQuery.trim() !== '' ||
      datePreset !== 'ALL' ||
      customFrom !== '' ||
      customTo !== '' ||
      sortKey !== 'created' ||
      sortDirection !== 'desc'
    );
  }, [stateFilter, severityFilter, searchQuery, datePreset, customFrom, customTo, sortKey, sortDirection]);

  const handleResetFilters = () => {
    setStateFilter('ALL');
    setSeverityFilter('ALL');
    setSearchQuery('');
    setDatePreset('ALL');
    setCustomFrom('');
    setCustomTo('');
    setSortKey('created');
    setSortDirection('desc');
  };

  // Filtered & Sorted Incidents
  const processedIncidents = useMemo(() => {
    const now = Date.now();

    // 1. Filter
    const filtered = incidents.filter((inc) => {
      if (stateFilter !== 'ALL' && inc.state !== stateFilter) return false;
      if (severityFilter !== 'ALL' && inc.severity !== severityFilter) return false;
      
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        const matchId = inc.incident_id.toLowerCase().includes(q);
        const matchService = inc.affected_service.toLowerCase().includes(q);
        const matchNs = (inc.affected_namespace || '').toLowerCase().includes(q);
        if (!matchId && !matchService && !matchNs) return false;
      }

      // Datetime filtering
      if (datePreset !== 'ALL') {
        const createdMs = new Date(inc.created_at).getTime();
        if (datePreset === '15M' && createdMs < now - 15 * 60 * 1000) return false;
        if (datePreset === '1H' && createdMs < now - 60 * 60 * 1000) return false;
        if (datePreset === '24H' && createdMs < now - 24 * 60 * 60 * 1000) return false;
        if (datePreset === '7D' && createdMs < now - 7 * 24 * 60 * 60 * 1000) return false;
        if (datePreset === 'CUSTOM') {
          if (customFrom && createdMs < new Date(customFrom).getTime()) return false;
          if (customTo && createdMs > new Date(customTo).getTime()) return false;
        }
      }

      return true;
    });

    // 2. Sort
    return [...filtered].sort((a, b) => {
      let cmp = 0;
      switch (sortKey) {
        case 'id':
          cmp = a.incident_id.localeCompare(b.incident_id);
          break;
        case 'service':
          cmp = a.affected_service.localeCompare(b.affected_service);
          break;
        case 'state':
          cmp = a.state.localeCompare(b.state);
          break;
        case 'severity': {
          const score = (s: string) => (s === 'P1' ? 3 : s === 'P2' ? 2 : 1);
          cmp = score(a.severity) - score(b.severity);
          break;
        }
        case 'created':
          cmp = new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
          break;
        case 'duration': {
          const durA = getIncidentDurationMs(a, now);
          const durB = getIncidentDurationMs(b, now);
          cmp = durA - durB;
          break;
        }
      }
      return sortDirection === 'asc' ? cmp : -cmp;
    });
  }, [incidents, stateFilter, severityFilter, searchQuery, datePreset, customFrom, customTo, sortKey, sortDirection]);

  const getSeverityBadgeClass = (sev: string) => {
    switch (sev) {
      case 'P1': return 'badge-p1';
      case 'P2': return 'badge-p2';
      default: return 'badge-p3';
    }
  };

  const getStateBadgeClass = (st: string) => {
    switch (st) {
      case 'AWAITING_APPROVAL': return 'badge-state-approval';
      case 'REMEDIATION':
      case 'EXECUTING':
      case 'INVESTIGATING': return 'badge-state-active';
      case 'STANDBY': return 'badge-state-standby';
      case 'ESCALATED': return 'badge-state-escalated';
      case 'RESOLVED':
      case 'CLOSED': return 'badge-state-resolved';
      default: return 'badge-state';
    }
  };

  const getIncidentDurationMs = (inc: Incident, referenceNow: number): number => {
    const created = new Date(inc.created_at).getTime();
    if (isNaN(created)) return 0;
    const isTerminal = inc.state === 'CLOSED' || inc.state === 'RESOLVED';
    const end = isTerminal && inc.updated_at ? new Date(inc.updated_at).getTime() : referenceNow;
    return Math.max(0, end - created);
  };

  const formatDuration = (inc: Incident) => {
    try {
      const diffMs = getIncidentDurationMs(inc, Date.now());
      const diffSec = Math.floor(diffMs / 1000);
      if (diffSec < 60) return `${diffSec}s`;
      if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ${diffSec % 60}s`;
      return `${Math.floor(diffSec / 3600)}h ${Math.floor((diffSec % 3600) / 60)}m`;
    } catch {
      return '—';
    }
  };

  const renderSortIndicator = (key: SortKey) => {
    if (sortKey !== key) {
      return <ArrowUpDown size={12} color="#64748b" style={{ opacity: 0.6 }} />;
    }
    return sortDirection === 'asc' ? (
      <ArrowUp size={13} color="#38bdf8" />
    ) : (
      <ArrowDown size={13} color="#38bdf8" />
    );
  };

  // Execute Deny
  const handleConfirmDeny = async () => {
    if (!denyTargetIncident) return;
    setDenySubmitting(true);
    try {
      const reason = denyReason.trim() || 'Operator invoked Deny from Fleet View';
      try {
        const act = await getIncidentAction(denyTargetIncident);
        if (act?.action_id) {
          await approveAction(denyTargetIncident, act.action_id, 'denied', reason);
        } else {
          await escalateIncident(denyTargetIncident, reason);
        }
      } catch {
        await escalateIncident(denyTargetIncident, reason);
      }
      setDenyTargetIncident(null);
      setDenyReason('');
      onRefresh();
    } catch (err: any) {
      alert(`Deny failed: ${err.message}`);
    } finally {
      setDenySubmitting(false);
    }
  };

  // Execute Drop Issue
  const handleConfirmDrop = async () => {
    if (!dropTargetIncident) return;
    setDropSubmitting(true);
    try {
      const reason = dropReason.trim() || 'Operator dropped incident from Fleet View';
      await dropIncident(dropTargetIncident, reason);
      setDropTargetIncident(null);
      setDropReason('');
      onRefresh();
    } catch (err: any) {
      alert(`Drop failed: ${err.message}`);
    } finally {
      setDropSubmitting(false);
    }
  };

  return (
    <div style={{ padding: '0 24px 24px 24px' }}>
      {/* Control Bar: Filters, Date Range, Sort Selector, and Auto-Refresh */}
      <div className="glass-panel" style={{
        padding: '16px 20px',
        marginBottom: '16px',
        display: 'flex',
        flexDirection: 'column',
        gap: '14px',
        borderRadius: '12px',
      }}>
        {/* Top Row: Primary Filters, Search, and Status Controls */}
        <div style={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '14px',
        }}>
          {/* Left: Search & Core Filters */}
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '10px' }}>
            {/* Search Input */}
            <div style={{ position: 'relative', minWidth: '220px' }}>
              <span style={{ position: 'absolute', left: '10px', top: '50%', transform: 'translateY(-50%)', color: '#64748b' }}>
                <Search size={14} />
              </span>
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Filter by ID, service, ns..."
                style={{
                  width: '100%',
                  padding: '8px 10px 8px 32px',
                  backgroundColor: 'rgba(15, 23, 42, 0.6)',
                  border: '1px solid var(--border-medium)',
                  borderRadius: '6px',
                  color: '#f8fafc',
                  fontSize: '0.825rem',
                  outline: 'none',
                }}
              />
            </div>

            {/* Severity Filter */}
            <select
              value={severityFilter}
              onChange={(e) => setSeverityFilter(e.target.value)}
              style={{
                padding: '8px 12px',
                backgroundColor: '#0f172a',
                border: '1px solid var(--border-medium)',
                borderRadius: '6px',
                color: '#f8fafc',
                fontSize: '0.825rem',
                outline: 'none',
                cursor: 'pointer',
              }}
            >
              <option value="ALL">All Severities</option>
              <option value="P1">P1 (Critical)</option>
              <option value="P2">P2 (High)</option>
              <option value="P3">P3 (Medium/Low)</option>
            </select>

            {/* State Filter */}
            <select
              value={stateFilter}
              onChange={(e) => setStateFilter(e.target.value)}
              style={{
                padding: '8px 12px',
                backgroundColor: '#0f172a',
                border: '1px solid var(--border-medium)',
                borderRadius: '6px',
                color: '#f8fafc',
                fontSize: '0.825rem',
                outline: 'none',
                cursor: 'pointer',
              }}
            >
              <option value="ALL">All States</option>
              <option value="AWAITING_APPROVAL">Awaiting Approval</option>
              <option value="REMEDIATION">Remediation</option>
              <option value="EXECUTING">Executing</option>
              <option value="MONITORING">Monitoring</option>
              <option value="STANDBY">Standby</option>
              <option value="ESCALATED">Escalated</option>
              <option value="RESOLVED">Resolved</option>
              <option value="CLOSED">Closed</option>
            </select>

            {/* Datetime Preset Filter */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <select
                value={datePreset}
                onChange={(e) => setDatePreset(e.target.value as DatePreset)}
                style={{
                  padding: '8px 12px',
                  backgroundColor: '#0f172a',
                  border: '1px solid var(--border-medium)',
                  borderRadius: '6px',
                  color: '#f8fafc',
                  fontSize: '0.825rem',
                  outline: 'none',
                  cursor: 'pointer',
                }}
              >
                <option value="ALL">All Time</option>
                <option value="15M">Past 15 Minutes</option>
                <option value="1H">Past 1 Hour</option>
                <option value="24H">Past 24 Hours</option>
                <option value="7D">Past 7 Days</option>
                <option value="CUSTOM">Custom Datetime Range...</option>
              </select>
            </div>

            {/* Sort Selector Dropdown */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <select
                value={currentSortPresetValue}
                onChange={(e) => handleSortDropdownChange(e.target.value)}
                style={{
                  padding: '8px 12px',
                  backgroundColor: '#0f172a',
                  border: '1px solid var(--border-medium)',
                  borderRadius: '6px',
                  color: '#f8fafc',
                  fontSize: '0.825rem',
                  outline: 'none',
                  cursor: 'pointer',
                }}
              >
                <option value="TIME_DESC">Created (Newest First)</option>
                <option value="TIME_ASC">Created (Oldest First)</option>
                <option value="DURATION_DESC">Duration (Longest First)</option>
                <option value="DURATION_ASC">Duration (Shortest First)</option>
                <option value="ALPHA_ASC">Incident ID (A-Z)</option>
                <option value="ALPHA_DESC">Incident ID (Z-A)</option>
                <option value="SEV_DESC">Severity (P1 → P3)</option>
                <option value="SEV_ASC">Severity (P3 → P1)</option>
                <option value="SERVICE_ASC">Service Name (A-Z)</option>
                <option value="SERVICE_DESC">Service Name (Z-A)</option>
                {currentSortPresetValue === 'CUSTOM' && (
                  <option value="CUSTOM">Custom Column Sort</option>
                )}
              </select>
            </div>
          </div>

          {/* Right: Refresh trigger */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
            <button
              onClick={onRefresh}
              className="btn btn-secondary btn-sm"
              disabled={loading}
              title="Refresh incident list now"
            >
              <RefreshCw size={13} className={loading ? 'pulse' : ''} />
              <span>{loading ? 'Refreshing...' : 'Refresh'}</span>
            </button>
          </div>
        </div>

        {/* Second Row: Custom Datetime Pickers (if CUSTOM selected) & Active Filter Indicator */}
        {(datePreset === 'CUSTOM' || isFiltered) && (
          <div style={{
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '12px',
            paddingTop: '10px',
            borderTop: '1px solid var(--border-subtle)',
          }}>
            {/* Custom Datetime Range Inputs */}
            {datePreset === 'CUSTOM' && (
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Calendar size={14} color="#38bdf8" />
                  <span style={{ fontSize: '0.75rem', color: '#94a3b8', fontWeight: 600 }}>From:</span>
                  <input
                    type="datetime-local"
                    value={customFrom}
                    onChange={(e) => setCustomFrom(e.target.value)}
                    style={{
                      padding: '5px 8px',
                      backgroundColor: '#0f172a',
                      border: '1px solid var(--border-medium)',
                      borderRadius: '5px',
                      color: '#f8fafc',
                      fontSize: '0.75rem',
                      outline: 'none',
                    }}
                  />
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span style={{ fontSize: '0.75rem', color: '#94a3b8', fontWeight: 600 }}>To:</span>
                  <input
                    type="datetime-local"
                    value={customTo}
                    onChange={(e) => setCustomTo(e.target.value)}
                    style={{
                      padding: '5px 8px',
                      backgroundColor: '#0f172a',
                      border: '1px solid var(--border-medium)',
                      borderRadius: '5px',
                      color: '#f8fafc',
                      fontSize: '0.75rem',
                      outline: 'none',
                    }}
                  />
                </div>
              </div>
            )}

            {/* Count & Reset Filters */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginLeft: 'auto' }}>
              <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                Showing <strong style={{ color: '#38bdf8' }}>{processedIncidents.length}</strong> of {incidents.length} incidents
              </span>

              {isFiltered && (
                <button
                  onClick={handleResetFilters}
                  className="btn btn-ghost btn-sm"
                  style={{
                    fontSize: '0.72rem',
                    padding: '3px 8px',
                    color: '#f87171',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                  }}
                  title="Clear all search queries, date limits, and sort overrides"
                >
                  <Filter size={12} />
                  <span>Reset All Filters</span>
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Incidents Table with Clickable Sort Headers */}
      <div className="glass-panel" style={{ overflow: 'hidden', borderRadius: '12px' }}>
        <table className="data-table">
          <thead>
            <tr>
              {/* Incident ID Header */}
              <th
                onClick={() => handleSortColumn('id')}
                style={{ cursor: 'pointer', userSelect: 'none' }}
                title="Sort by Incident ID (Click to toggle A-Z / Z-A)"
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span>Incident</span>
                  {renderSortIndicator('id')}
                </div>
              </th>

              {/* Severity Header */}
              <th
                onClick={() => handleSortColumn('severity')}
                style={{ cursor: 'pointer', userSelect: 'none' }}
                title="Sort by Severity (Click to toggle P1-P3 / P3-P1)"
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span>Severity</span>
                  {renderSortIndicator('severity')}
                </div>
              </th>

              {/* State Header */}
              <th
                onClick={() => handleSortColumn('state')}
                style={{ cursor: 'pointer', userSelect: 'none' }}
                title="Sort by State"
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span>State</span>
                  {renderSortIndicator('state')}
                </div>
              </th>

              {/* Affected Target Header */}
              <th
                onClick={() => handleSortColumn('service')}
                style={{ cursor: 'pointer', userSelect: 'none' }}
                title="Sort by Service Name"
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span>Affected Target</span>
                  {renderSortIndicator('service')}
                </div>
              </th>

              {/* Duration Header */}
              <th
                onClick={() => handleSortColumn('duration')}
                style={{ cursor: 'pointer', userSelect: 'none' }}
                title="Sort by Elapsed Duration (Click to toggle longest / shortest)"
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <span>Duration</span>
                  {renderSortIndicator('duration')}
                </div>
              </th>

              {/* Action Header */}
              <th style={{ width: '160px', textAlign: 'center' }}>
                Actions
              </th>
            </tr>
          </thead>
          <tbody>
            {processedIncidents.length === 0 ? (
              <tr>
                <td colSpan={6} style={{ textAlign: 'center', padding: '48px 16px', color: '#94a3b8' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '8px' }}>
                    <AlertTriangle size={24} color="#64748b" />
                    <p style={{ fontWeight: 500 }}>No incidents matching the current filters.</p>
                    <p style={{ fontSize: '0.75rem', color: '#64748b' }}>Try clearing filters or check alert ingestion queue.</p>
                  </div>
                </td>
              </tr>
            ) : (
              processedIncidents.map((inc) => (
                <tr
                  key={inc.incident_id}
                  className="clickable"
                  onClick={() => onSelectIncident(inc.incident_id)}
                >
                  {/* ID */}
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span className="mono" style={{ fontWeight: 600, color: '#38bdf8' }}>
                        {inc.incident_id}
                      </span>
                      {inc.state === 'AWAITING_APPROVAL' && (
                        <span style={{
                          backgroundColor: '#f59e0b',
                          color: '#000',
                          fontSize: '0.65rem',
                          fontWeight: 800,
                          padding: '1px 5px',
                          borderRadius: '4px',
                          letterSpacing: '0.04em',
                        }}>
                          ACTION NEEDED
                        </span>
                      )}
                    </div>
                  </td>

                  {/* Severity */}
                  <td>
                    <span className={`badge ${getSeverityBadgeClass(inc.severity)}`}>
                      {inc.severity}
                    </span>
                  </td>

                  {/* State */}
                  <td>
                    <span className={`badge ${getStateBadgeClass(inc.state)}`}>
                      {inc.state}
                    </span>
                  </td>

                  {/* Service & Namespace */}
                  <td>
                    <div style={{ display: 'flex', flexDirection: 'column' }}>
                      <span style={{ fontWeight: 600, color: '#f1f5f9' }}>
                        {inc.affected_service}
                      </span>
                      <span className="mono" style={{ fontSize: '0.72rem', color: '#64748b' }}>
                        ns: {inc.affected_namespace || 'default'}
                      </span>
                    </div>
                  </td>

                  {/* Duration */}
                  <td>
                    <span className="mono" style={{ fontSize: '0.8rem', color: '#cbd5e1' }}>
                      {formatDuration(inc)}
                    </span>
                  </td>

                  {/* Incident Actions */}
                  <td style={{ textAlign: 'center' }} onClick={(e) => e.stopPropagation()}>
                    <div style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap', justifyContent: 'center' }}>
                      {inc.has_report && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleOpenReport(inc.incident_id);
                          }}
                          disabled={reportModalLoading && reportModalIncidentId === inc.incident_id}
                          className="btn btn-sm"
                          style={{
                            backgroundColor: 'rgba(56, 189, 248, 0.15)',
                            border: '1px solid rgba(56, 189, 248, 0.4)',
                            color: '#38bdf8',
                            fontSize: '0.725rem',
                            padding: '3px 8px',
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '4px',
                            cursor: reportModalLoading && reportModalIncidentId === inc.incident_id ? 'wait' : 'pointer',
                          }}
                          title="View Incident Postmortem Report"
                        >
                          <span>{reportModalLoading && reportModalIncidentId === inc.incident_id ? '⏳ Loading...' : '📄 Report'}</span>
                        </button>
                      )}
                      {inc.state !== 'CLOSED' && inc.state !== 'RESOLVED' && (
                        <>
                          {inc.state !== 'ESCALATED' && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setDenyTargetIncident(inc.incident_id);
                                setDenyReason('');
                              }}
                              className="btn btn-sm"
                              style={{
                                backgroundColor: 'rgba(239, 68, 68, 0.15)',
                                border: '1px solid rgba(239, 68, 68, 0.4)',
                                color: '#fca5a5',
                                fontSize: '0.725rem',
                                padding: '3px 8px',
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: '4px',
                              }}
                              title="Deny actions and escalate"
                            >
                              <XCircle size={12} color="#ef4444" />
                              <span>Deny</span>
                            </button>
                          )}
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              setDropTargetIncident(inc.incident_id);
                              setDropReason('');
                            }}
                            className="btn btn-sm"
                            style={{
                              backgroundColor: 'rgba(244, 63, 94, 0.15)',
                              border: '1px solid rgba(244, 63, 94, 0.4)',
                              color: '#fda4af',
                              fontSize: '0.725rem',
                              padding: '3px 8px',
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '4px',
                            }}
                            title="Drop issue: halt system and agents on this incident and close it"
                          >
                            <StopCircle size={12} color="#f43f5e" />
                            <span>Drop</span>
                          </button>
                        </>
                      )}
                      {!inc.has_report && (inc.state === 'CLOSED' || inc.state === 'RESOLVED') && (
                        <span style={{ fontSize: '0.75rem', color: '#64748b' }}>—</span>
                      )}
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Deny Modal (Portal to body) */}
      {denyTargetIncident && typeof document !== 'undefined' && createPortal(
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
          <div className="glass-panel" style={{ width: '100%', maxWidth: '440px', padding: '24px', border: '1px solid rgba(239, 68, 68, 0.4)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
              <XCircle size={18} color="#ef4444" />
              <h3 style={{ fontSize: '1.05rem', fontWeight: 800, color: '#fca5a5' }}>
                Reject / Deny Remediation Action
              </h3>
            </div>
            <p style={{ fontSize: '0.825rem', color: '#cbd5e1', marginBottom: '14px', lineHeight: 1.5 }}>
              Denying remediation blocks write tools and escalates incident <strong className="mono" style={{ color: '#38bdf8' }}>{denyTargetIncident}</strong> to the human on-call bridge.
            </p>
            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', fontSize: '0.75rem', fontWeight: 600, color: '#94a3b8', marginBottom: '6px' }}>
                Denial Reason / Audit Note
              </label>
              <textarea
                value={denyReason}
                onChange={(e) => setDenyReason(e.target.value)}
                placeholder="e.g. Operator rejected pod restart due to active database freeze"
                rows={3}
                style={{
                  width: '100%',
                  padding: '8px 10px',
                  backgroundColor: 'rgba(7, 9, 14, 0.8)',
                  border: '1px solid var(--border-medium)',
                  borderRadius: '6px',
                  color: '#ffffff',
                  fontSize: '0.825rem',
                }}
              />
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                onClick={() => setDenyTargetIncident(null)}
                className="btn btn-ghost"
                disabled={denySubmitting}
              >
                Cancel
              </button>
              <button
                onClick={handleConfirmDeny}
                className="btn btn-danger"
                disabled={denySubmitting}
                style={{ backgroundColor: '#dc2626' }}
              >
                {denySubmitting ? 'Denying...' : 'Confirm Deny'}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* Drop Issue Modal (Portal to body) */}
      {dropTargetIncident && typeof document !== 'undefined' && createPortal(
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
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
              <StopCircle size={18} color="#f43f5e" />
              <h3 style={{ fontSize: '1.05rem', fontWeight: 800, color: '#fda4af' }}>
                Drop Incident — Halt Agent Work
              </h3>
            </div>
            <p style={{ fontSize: '0.825rem', color: '#cbd5e1', marginBottom: '14px', lineHeight: 1.5 }}>
              Dropping incident <strong className="mono" style={{ color: '#38bdf8' }}>{dropTargetIncident}</strong> halts all automated agent workflows, cancels pending actions, and sets the incident state to <strong style={{ color: '#10b981' }}>CLOSED</strong>.
            </p>
            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', fontSize: '0.75rem', fontWeight: 600, color: '#94a3b8', marginBottom: '6px' }}>
                Drop Reason / Note (Optional)
              </label>
              <textarea
                value={dropReason}
                onChange={(e) => setDropReason(e.target.value)}
                placeholder="e.g. False positive, duplicate issue, or external fix applied"
                rows={3}
                style={{
                  width: '100%',
                  padding: '8px 10px',
                  backgroundColor: 'rgba(7, 9, 14, 0.8)',
                  border: '1px solid var(--border-medium)',
                  borderRadius: '6px',
                  color: '#ffffff',
                  fontSize: '0.825rem',
                }}
              />
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                onClick={() => setDropTargetIncident(null)}
                className="btn btn-ghost"
                disabled={dropSubmitting}
              >
                Cancel
              </button>
              <button
                onClick={handleConfirmDrop}
                className="btn btn-danger"
                disabled={dropSubmitting}
                style={{ backgroundColor: '#e11d48' }}
              >
                {dropSubmitting ? 'Dropping...' : 'Confirm Drop'}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* Report Modal (Portal to body) */}
      {reportModalIncidentId && reportModalContent && typeof document !== 'undefined' && createPortal(
        <IncidentReportViewer
          incidentId={reportModalIncidentId}
          markdownContent={reportModalContent}
          isModal={true}
          onClose={() => {
            setReportModalIncidentId(null);
            setReportModalContent(null);
          }}
        />,
        document.body
      )}
    </div>
  );
};
