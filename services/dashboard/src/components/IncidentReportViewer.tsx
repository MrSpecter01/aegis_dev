import React, { useState } from 'react';

interface IncidentReportViewerProps {
  incidentId: string;
  markdownContent: string;
  onClose?: () => void;
  isModal?: boolean;
}

export const IncidentReportViewer: React.FC<IncidentReportViewerProps> = ({
  incidentId,
  markdownContent,
  onClose,
  isModal = false,
}) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(markdownContent);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleDownload = () => {
    const blob = new Blob([markdownContent], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `incident-report-${incidentId}.md`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  // Helper to parse markdown inline styles (code, bold, italic, <sub>, checkboxes)
  const formatInline = (text: string): React.ReactNode[] => {
    const parts: React.ReactNode[] = [];
    let remaining = text.replace(/&nbsp;/g, '\u00A0').replace(/\\\|/g, '|');
    let keyIdx = 0;

    while (remaining) {
      // Check for HTML <sub>...</sub>
      const subMatch = remaining.match(/^<sub>([\s\S]*?)<\/sub>/i);
      if (subMatch) {
        parts.push(
          <span
            key={keyIdx++}
            style={{
              fontSize: '0.8rem',
              color: '#94a3b8',
              display: 'block',
              marginTop: '3px',
            }}
          >
            {formatInline(subMatch[1])}
          </span>
        );
        remaining = remaining.slice(subMatch[0].length);
        continue;
      }

      // Check for code `...`
      const codeMatch = remaining.match(/^`([^`]+)`/);
      if (codeMatch) {
        parts.push(
          <code
            key={keyIdx++}
            style={{
              backgroundColor: 'rgba(56, 189, 248, 0.1)',
              color: '#38bdf8',
              padding: '2px 5px',
              borderRadius: '4px',
              fontFamily: 'monospace',
              fontSize: '0.85em',
            }}
          >
            {codeMatch[1]}
          </code>
        );
        remaining = remaining.slice(codeMatch[0].length);
        continue;
      }

      // Check for bold **...**
      const boldMatch = remaining.match(/^\*\*([^*]+)\*\*/);
      if (boldMatch) {
        parts.push(
          <strong key={keyIdx++} style={{ color: '#f8fafc', fontWeight: 700 }}>
            {boldMatch[1]}
          </strong>
        );
        remaining = remaining.slice(boldMatch[0].length);
        continue;
      }

      // Check for italic *...*
      const italicMatch = remaining.match(/^\*([^*\n]+)\*/);
      if (italicMatch) {
        parts.push(
          <em key={keyIdx++} style={{ color: '#cbd5e1', fontStyle: 'italic' }}>
            {formatInline(italicMatch[1])}
          </em>
        );
        remaining = remaining.slice(italicMatch[0].length);
        continue;
      }

      // Check for checkbox [ ]
      const checkboxMatch = remaining.match(/^\[([ xX✓*vV]*)\]/);
      if (checkboxMatch) {
        const isChecked = checkboxMatch[1].trim().length > 0;
        parts.push(
          <span
            key={keyIdx++}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: '14px',
              height: '14px',
              border: '1.5px solid #64748b',
              borderRadius: '3px',
              marginRight: '4px',
              verticalAlign: 'middle',
              backgroundColor: isChecked ? '#38bdf8' : 'transparent',
              color: '#0f172a',
              fontSize: '10px',
              fontWeight: 800,
            }}
          >
            {isChecked ? '✓' : ''}
          </span>
        );
        remaining = remaining.slice(checkboxMatch[0].length);
        continue;
      }

      // Plain text character or chunk up to next special char
      const nextSpecial = remaining.search(/[`*\[<]/);
      if (nextSpecial === -1) {
        parts.push(remaining);
        break;
      } else if (nextSpecial === 0) {
        parts.push(remaining[0]);
        remaining = remaining.slice(1);
      } else {
        parts.push(remaining.slice(0, nextSpecial));
        remaining = remaining.slice(nextSpecial);
      }
    }

    return parts;
  };

  // Structured parser for markdown sections & tables
  const renderMarkdownBlocks = () => {
    const lines = markdownContent.split('\n');
    const elements: React.ReactNode[] = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // H1 Header
      if (line.startsWith('# ')) {
        elements.push(
          <div
            key={i}
            style={{
              paddingBottom: '12px',
              marginBottom: '20px',
              borderBottom: '1px solid var(--border-medium)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <h1 style={{ fontSize: '1.5rem', fontWeight: 800, color: '#f8fafc', margin: 0 }}>
              {line.replace('# ', '')}
            </h1>
            <span
              style={{
                backgroundColor: 'rgba(16, 185, 129, 0.15)',
                color: '#34d399',
                border: '1px solid rgba(16, 185, 129, 0.3)',
                padding: '4px 10px',
                borderRadius: '6px',
                fontSize: '0.75rem',
                fontWeight: 700,
                letterSpacing: '0.04em',
              }}
            >
              VERIFIED REPORT
            </span>
          </div>
        );
        i++;
        continue;
      }

      // H2 Header
      if (line.startsWith('## ')) {
        const title = line.replace('## ', '');
        elements.push(
          <h2
            key={i}
            style={{
              fontSize: '1.15rem',
              fontWeight: 700,
              color: '#38bdf8',
              marginTop: '28px',
              marginBottom: '12px',
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
            }}
          >
            <span>{formatInline(title)}</span>
          </h2>
        );
        i++;
        continue;
      }

      // H3 Header
      if (line.startsWith('### ')) {
        const title = line.replace('### ', '');
        elements.push(
          <h3
            key={i}
            style={{
              fontSize: '1rem',
              fontWeight: 700,
              color: '#e2e8f0',
              marginTop: '18px',
              marginBottom: '8px',
            }}
          >
            {formatInline(title)}
          </h3>
        );
        i++;
        continue;
      }

      // H4 Header
      if (line.startsWith('#### ')) {
        const title = line.replace('#### ', '');
        elements.push(
          <h4
            key={i}
            style={{
              fontSize: '0.9rem',
              fontWeight: 700,
              color: '#cbd5e1',
              marginTop: '14px',
              marginBottom: '6px',
            }}
          >
            {formatInline(title)}
          </h4>
        );
        i++;
        continue;
      }

      // Blockquote group (> ...)
      if (line.trimStart().startsWith('>')) {
        const quoteLines: string[] = [];
        const startIdx = i;
        while (i < lines.length && lines[i].trimStart().startsWith('>')) {
          quoteLines.push(lines[i].trimStart().replace(/^>\s?/, ''));
          i++;
        }
        elements.push(
          <div
            key={`bq-${startIdx}`}
            style={{
              margin: '12px 0',
              padding: '10px 14px',
              borderLeft: '3px solid #38bdf8',
              backgroundColor: 'rgba(56, 189, 248, 0.08)',
              borderRadius: '0 8px 8px 0',
              fontSize: '0.85rem',
              color: '#f1f5f9',
              lineHeight: 1.5,
            }}
          >
            {quoteLines.map((ql, qIdx) => (
              <div key={qIdx} style={{ marginTop: qIdx > 0 ? '4px' : 0 }}>
                {formatInline(ql)}
              </div>
            ))}
          </div>
        );
        continue;
      }

      // Table parsing
      if (line.trim().startsWith('|') && line.trim().endsWith('|')) {
        const tableLines: string[] = [];
        while (i < lines.length && lines[i].trim().startsWith('|') && lines[i].trim().endsWith('|')) {
          tableLines.push(lines[i].trim());
          i++;
        }

        if (tableLines.length >= 2) {
          const splitRow = (r: string) => r.split(/(?<!\\)\|/).slice(1, -1).map((c) => c.trim());
          const headerCells = splitRow(tableLines[0]);
          const rows = tableLines.slice(2).map(splitRow);

          elements.push(
            <div
              key={`tbl-${i}`}
              style={{
                overflowX: 'auto',
                margin: '14px 0',
                borderRadius: '8px',
                border: '1px solid var(--border-medium)',
                backgroundColor: 'rgba(15, 23, 42, 0.6)',
              }}
            >
              <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '0.82rem' }}>
                <thead>
                  <tr style={{ backgroundColor: 'rgba(30, 41, 59, 0.8)', borderBottom: '1px solid var(--border-medium)' }}>
                    {headerCells.map((h, hIdx) => (
                      <th key={hIdx} style={{ padding: '8px 12px', fontWeight: 600, color: '#94a3b8' }}>
                        {formatInline(h)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, rIdx) => (
                    <tr
                      key={rIdx}
                      style={{
                        borderBottom: rIdx < rows.length - 1 ? '1px solid rgba(51, 65, 85, 0.4)' : 'none',
                        transition: 'background-color 0.15s ease',
                      }}
                    >
                      {row.map((cell, cIdx) => (
                        <td key={cIdx} style={{ padding: '8px 12px', color: '#e2e8f0', verticalAlign: 'top' }}>
                          {formatInline(cell)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
          continue;
        }
      }

      // Bullet List Item (preserving nested indentation)
      if (line.trimStart().startsWith('- ') || line.trimStart().startsWith('* ')) {
        const leadingSpaces = line.length - line.trimStart().length;
        const bulletText = line.trimStart().replace(/^[-*]\s+/, '');
        elements.push(
          <div
            key={i}
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: '8px',
              margin: '4px 0',
              paddingLeft: `${8 + Math.min(leadingSpaces * 8, 48)}px`,
              fontSize: '0.85rem',
              color: '#cbd5e1',
            }}
          >
            <span style={{ color: leadingSpaces > 0 ? '#64748b' : '#38bdf8', fontSize: '1rem', lineHeight: '1.2' }}>•</span>
            <div style={{ flex: 1, lineHeight: '1.5' }}>{formatInline(bulletText)}</div>
          </div>
        );
        i++;
        continue;
      }

      // Numbered List Item
      const numMatch = line.match(/^(\s*)(\d+)\.\s+(.*)$/);
      if (numMatch) {
        const leadingSpaces = numMatch[1].length;
        elements.push(
          <div
            key={i}
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: '8px',
              margin: '4px 0',
              paddingLeft: `${8 + Math.min(leadingSpaces * 8, 48)}px`,
              fontSize: '0.85rem',
              color: '#cbd5e1',
            }}
          >
            <span className="mono" style={{ color: '#38bdf8', fontWeight: 600, minWidth: '18px' }}>{numMatch[2]}.</span>
            <div style={{ flex: 1, lineHeight: '1.5' }}>{formatInline(numMatch[3])}</div>
          </div>
        );
        i++;
        continue;
      }

      // Empty line
      if (!line.trim()) {
        i++;
        continue;
      }

      // Standard Paragraph
      elements.push(
        <p key={i} style={{ margin: '6px 0', fontSize: '0.85rem', color: '#cbd5e1', lineHeight: '1.6' }}>
          {formatInline(line)}
        </p>
      );
      i++;
    }

    return elements;
  };

  const contentElement = (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        maxHeight: isModal ? '80vh' : 'auto',
        overflow: 'hidden',
      }}
    >
      {/* Top Action Toolbar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 18px',
          backgroundColor: 'rgba(15, 23, 42, 0.8)',
          borderBottom: '1px solid var(--border-medium)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ fontSize: '0.8rem', color: '#94a3b8' }}>Postmortem Artifact:</span>
          <span className="mono" style={{ fontSize: '0.8rem', color: '#38bdf8', fontWeight: 600 }}>
            report.md
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <button
            onClick={handleCopy}
            className="btn btn-secondary btn-sm"
            style={{ fontSize: '0.75rem', padding: '4px 10px' }}
            title="Copy raw markdown to clipboard"
          >
            {copied ? '✓ Copied!' : '📋 Copy Markdown'}
          </button>
          <button
            onClick={handleDownload}
            className="btn btn-primary btn-sm"
            style={{ fontSize: '0.75rem', padding: '4px 10px' }}
            title="Download report as .md"
          >
            ⬇️ Download .md
          </button>
          {isModal && onClose && (
            <button
              onClick={onClose}
              className="btn btn-ghost btn-sm"
              style={{ fontSize: '0.8rem', padding: '4px 8px', color: '#94a3b8' }}
              title="Close viewer"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* Rendered Document Body */}
      <div
        style={{
          padding: '24px',
          overflowY: 'auto',
          backgroundColor: 'rgba(10, 15, 29, 0.95)',
        }}
      >
        {renderMarkdownBlocks()}
      </div>
    </div>
  );

  if (isModal) {
    return (
      <div
        style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          backgroundColor: 'rgba(0, 0, 0, 0.75)',
          backdropFilter: 'blur(4px)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 1000,
          padding: '20px',
        }}
        onClick={onClose}
      >
        <div
          className="glass-panel"
          style={{
            width: '100%',
            maxWidth: '900px',
            borderRadius: '14px',
            overflow: 'hidden',
            boxShadow: '0 20px 40px rgba(0, 0, 0, 0.6)',
            border: '1px solid var(--border-medium)',
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {contentElement}
        </div>
      </div>
    );
  }

  return (
    <div
      className="glass-panel"
      style={{
        borderRadius: '12px',
        overflow: 'hidden',
        border: '1px solid var(--border-medium)',
        marginTop: '16px',
      }}
    >
      {contentElement}
    </div>
  );
};
