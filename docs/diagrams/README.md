# Aegis Architecture Diagrams

This directory contains the public, high-level architecture views for **Aegis**. Each diagram focuses on a distinct architectural perspective while staying consistent with the core system boundaries.

> **Tip:** Static `.svg` previews render directly in Markdown viewers. For dark/light theme toggling, guided focus views, zoom/pan, and PNG/SVG export, open the corresponding interactive `.html` file locally in a browser.

---

## 1. System Architecture Overview

One-page view of entry points, major subsystems, trust boundaries, external systems, and primary communication paths.

![Aegis — System Architecture Overview](system-overview.svg)

- **Interactive HTML:** [`system-overview.html`](system-overview.html)

---

## 2. Application & Backend Architecture

Zooms into the application and backend layer—showing the Control Plane separation, stream-driven async phase workers, shared harness boundaries, and policy-mediated tool execution.

![Aegis — Application & Backend Architecture](application-backend.svg)

- **Interactive HTML:** [`application-backend.html`](application-backend.html)

---

## 3. Data & Integration Architecture

Shows how alerts, operator actions, telemetry, vectors, and durable incident artifacts move across the Redis event bus, primary data stores, and external integrations.

![Aegis — Data & Integration Architecture](data-integration.svg)

- **Interactive HTML:** [`data-integration.html`](data-integration.html)

---

## 4. Business Capabilities & System Interactions

Maps user roles and external triggers to user-facing capabilities, supporting internal subsystems, and external ecosystem integrations.

![Aegis — Business Capabilities & System Interactions](capabilities-interactions.svg)

- **Interactive HTML:** [`capabilities-interactions.html`](capabilities-interactions.html)

