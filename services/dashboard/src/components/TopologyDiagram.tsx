import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { TopologyGraph, TopologyNode, TopologyEdge } from '../types';
import { Layers, RefreshCw } from './Icons';

interface TopologyDiagramProps {
  graph: TopologyGraph | null;
  affectedService?: string;
  loading?: boolean;
  initialNamespace?: string;
  hideNamespaceDropdown?: boolean;
  onNamespaceChange?: (namespace: string) => void;
}

export type NodeCategory =
  | 'affected'
  | 'gateway'
  | 'service'
  | 'datastore'
  | 'queue'
  | 'infra'
  | 'external'
  | 'unknown';

export interface CategoryStyle {
  label: string;
  color: string;
  glow: string;
  bg: string;
  border: string;
}

export const CATEGORY_STYLES: Record<NodeCategory, CategoryStyle> = {
  affected: {
    label: 'Affected / Root',
    color: '#f43f5e',
    glow: 'rgba(244, 63, 94, 0.65)',
    bg: 'rgba(244, 63, 94, 0.22)',
    border: '#f43f5e',
  },
  gateway: {
    label: 'Ingress / Gateway',
    color: '#10b981',
    glow: 'rgba(16, 185, 129, 0.55)',
    bg: 'rgba(16, 185, 129, 0.18)',
    border: '#10b981',
  },
  service: {
    label: 'Core Service',
    color: '#38bdf8',
    glow: 'rgba(56, 189, 248, 0.55)',
    bg: 'rgba(56, 189, 248, 0.18)',
    border: '#38bdf8',
  },
  datastore: {
    label: 'Datastore / Cache',
    color: '#a855f7',
    glow: 'rgba(168, 85, 247, 0.55)',
    bg: 'rgba(168, 85, 247, 0.2)',
    border: '#a855f7',
  },
  queue: {
    label: 'Message Queue / Stream',
    color: '#f59e0b',
    glow: 'rgba(245, 158, 11, 0.55)',
    bg: 'rgba(245, 158, 11, 0.2)',
    border: '#f59e0b',
  },
  infra: {
    label: 'Telemetry / Platform',
    color: '#06b6d4',
    glow: 'rgba(6, 182, 212, 0.55)',
    bg: 'rgba(6, 182, 212, 0.16)',
    border: '#06b6d4',
  },
  external: {
    label: 'External / Third-Party',
    color: '#ec4899',
    glow: 'rgba(236, 72, 153, 0.55)',
    bg: 'rgba(236, 72, 153, 0.18)',
    border: '#ec4899',
  },
  unknown: {
    label: 'General / Unknown',
    color: '#94a3b8',
    glow: 'rgba(148, 163, 184, 0.45)',
    bg: 'rgba(148, 163, 184, 0.15)',
    border: '#94a3b8',
  },
};

export function classifyNode(node: TopologyNode, isAffected: boolean): NodeCategory {
  if (isAffected) return 'affected';
  const name = (node.name || '').toLowerCase();
  const kind = (node.kind || '').toLowerCase();
  const type = (node.type || '').toLowerCase();

  // Datastore & Cache
  if (
    /(redis|valkey|db|database|postgres|postgresql|mysql|sqlite|mongo|mongodb|cassandra|elasticsearch|opensearch|memcached|etcd|dynamo)/.test(name) ||
    /database|datastore/.test(type) ||
    /database|datastore/.test(kind)
  ) {
    return 'datastore';
  }

  // Queue & Streaming
  if (
    /(kafka|rabbit|rabbitmq|queue|mq|nats|pubsub|event|stream|sqs|kinesis|celery|activemq)/.test(name) ||
    /queue|messaging|streaming/.test(type)
  ) {
    return 'queue';
  }

  // Ingress & Gateway & Frontend
  if (
    /(frontend|ingress|gateway|proxy|router|web|ui|api-gateway|envoy|nginx|traefik|kong|istio)/.test(name) ||
    /gateway|ingress|loadbalancer/.test(type)
  ) {
    return 'gateway';
  }

  // External APIs & Services
  if (/(external|thirdparty|stripe|paypal|auth0|twilio|sendgrid|vendor)/.test(name) || /external/.test(kind)) {
    return 'external';
  }

  // Telemetry & Infrastructure
  if (
    node.is_stub ||
    /(collector|otel|prometheus|jaeger|tempo|grafana|alertmanager|loki|fluent|fluentd|agent|sidecar|telemetry|metrics)/.test(name) ||
    /infra|telemetry|monitoring/.test(kind)
  ) {
    return 'infra';
  }

  // Microservices & Backend applications
  if (/(service|server|api|app|worker|handler|controller|checkout|cart|order|user|payment|auth|catalog|product|account|shipping)/.test(name) || type === 'Deployment' || type === 'StatefulSet') {
    return 'service';
  }

  return 'unknown';
}

interface SimNode {
  id: string;
  name: string;
  category: NodeCategory;
  raw: TopologyNode;
  x: number;
  y: number;
  vx: number;
  vy: number;
  fx: number | null;
  fy: number | null;
  radius: number;
  degree: number;
}

interface SimEdge {
  id: string;
  source: string;
  target: string;
  raw: TopologyEdge;
}

export const TopologyDiagram: React.FC<TopologyDiagramProps> = ({
  graph,
  affectedService,
  loading,
  initialNamespace,
  hideNamespaceDropdown = false,
  onNamespaceChange,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<NodeCategory | null>(null);

  // Active observed namespaces (only those currently being observed)
  const observedNamespaces = useMemo(() => {
    if (graph?.observed_namespaces && graph.observed_namespaces.length > 0) {
      return graph.observed_namespaces;
    }
    // Fallback: if not supplied, detect from graph nodes or default to default
    const set = new Set<string>();
    if (graph?.nodes) {
      graph.nodes.forEach((n) => {
        if (n.namespace && n.namespace.toLowerCase() !== 'default') {
          set.add(n.namespace);
        }
      });
    }
    return set.size > 0 ? Array.from(set).sort() : ['default'];
  }, [graph]);

  // Namespace filtering state: defaults to initialNamespace or current observed
  const [selectedNamespace, setSelectedNamespace] = useState<string>(
    () => initialNamespace || graph?.current_namespace || 'default'
  );

  useEffect(() => {
    if (initialNamespace) {
      setSelectedNamespace(initialNamespace);
    } else if (graph?.current_namespace && graph.current_namespace !== 'ALL') {
      setSelectedNamespace(graph.current_namespace);
    } else if (observedNamespaces.length > 0 && !observedNamespaces.includes(selectedNamespace)) {
      setSelectedNamespace(observedNamespaces[0]);
    }
  }, [initialNamespace, graph, observedNamespaces, selectedNamespace]);

  // Pan and Zoom viewport state
  const [zoom, setZoom] = useState<number>(1);
  const [pan, setPan] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState<boolean>(false);
  const [panStart, setPanStart] = useState<{ x: number; y: number }>({ x: 0, y: 0 });

  // Dragging node state
  const [draggedNodeId, setDraggedNodeId] = useState<string | null>(null);
  const dragStartOffsetRef = useRef<{ offsetX: number; offsetY: number }>({ offsetX: 0, offsetY: 0 });

  const width = 960;
  const height = 540;
  const centerX = width / 2;
  const centerY = height / 2;

  const getFrom = useCallback((e: any) => e.from || e.from_node || e.source || '', []);
  const getTo = useCallback((e: any) => e.to || e.to_node || e.target || '', []);

  const rawNodes = useMemo(() => {
    const all = graph?.nodes || [];
    return all.filter((n) => (n.namespace || '').toLowerCase() === selectedNamespace.toLowerCase());
  }, [graph, selectedNamespace]);

  const rawEdges = useMemo(() => {
    const all = graph?.edges || [];
    const nodeIds = new Set(rawNodes.map((n) => n.id));
    return all.filter((e) => {
      const fromId = getFrom(e);
      const toId = getTo(e);
      return nodeIds.has(fromId) && nodeIds.has(toId);
    });
  }, [graph, selectedNamespace, rawNodes, getFrom, getTo]);

  // Simulation physics objects stored in refs
  const simNodesRef = useRef<SimNode[]>([]);
  const simEdgesRef = useRef<SimEdge[]>([]);
  const animFrameRef = useRef<number | null>(null);
  const alphaRef = useRef<number>(1.0);
  const lastTickTimeRef = useRef<number>(0);

  // Force re-render state
  const [, setFrameTick] = useState<number>(0);

  // Categorize raw nodes
  const nodeCategoryMap = useMemo(() => {
    const map = new Map<string, NodeCategory>();
    rawNodes.forEach((node) => {
      const isAffected = Boolean(
        affectedService && node.name.toLowerCase() === affectedService.toLowerCase()
      );
      map.set(node.id, classifyNode(node, isAffected));
    });
    return map;
  }, [rawNodes, affectedService]);

  // Count occurrences per category for legend badges
  const categoryCounts = useMemo(() => {
    const counts: Record<NodeCategory, number> = {
      affected: 0,
      gateway: 0,
      service: 0,
      datastore: 0,
      queue: 0,
      infra: 0,
      external: 0,
      unknown: 0,
    };
    nodeCategoryMap.forEach((cat) => {
      counts[cat] = (counts[cat] || 0) + 1;
    });
    return counts;
  }, [nodeCategoryMap]);

  // Compute degree counts (in/out) for raw edges
  const { nodeDegreeMap, directNeighborMap, activeEdgeMap } = useMemo(() => {
    const inDeg: Record<string, number> = {};
    const outDeg: Record<string, number> = {};
    const neighborMap = new Map<string, Set<string>>();
    const edgeMap = new Map<string, Set<string>>();

    rawNodes.forEach((n) => {
      inDeg[n.id] = 0;
      outDeg[n.id] = 0;
      neighborMap.set(n.id, new Set<string>());
      edgeMap.set(n.id, new Set<string>());
    });

    rawEdges.forEach((edge, idx) => {
      const src = getFrom(edge);
      const tgt = getTo(edge);
      const edgeId = `edge-${idx}-${src}-${tgt}`;
      if (src && tgt) {
        if (outDeg[src] !== undefined) outDeg[src]++;
        if (inDeg[tgt] !== undefined) inDeg[tgt]++;

        neighborMap.get(src)?.add(tgt);
        neighborMap.get(tgt)?.add(src);
        edgeMap.get(src)?.add(edgeId);
        edgeMap.get(tgt)?.add(edgeId);
      }
    });

    return {
      nodeDegreeMap: { inDeg, outDeg },
      directNeighborMap: neighborMap,
      activeEdgeMap: edgeMap,
    };
  }, [rawNodes, rawEdges, getFrom, getTo]);

  // Initialize simulation nodes and edges once or on graph change
  useEffect(() => {
    if (rawNodes.length === 0) {
      simNodesRef.current = [];
      simEdgesRef.current = [];
      return;
    }

    const n = rawNodes.length;
    const initialNodes: SimNode[] = rawNodes.map((node, i) => {
      const cat = nodeCategoryMap.get(node.id) || 'unknown';
      const isAff = cat === 'affected';
      const inD = nodeDegreeMap.inDeg[node.id] || 0;
      const outD = nodeDegreeMap.outDeg[node.id] || 0;
      const totalDeg = inD + outD;

      // Smart initial positioning:
      // Connected nodes clustered in center; isolated/standalone (degree 0) placed gently along outer rim
      let initialX = centerX;
      let initialY = centerY;

      if (isAff) {
        initialX = centerX;
        initialY = centerY;
      } else if (totalDeg === 0) {
        // Isolated/standalone nodes placed along outer perimeter
        const angle = (i / Math.max(1, n)) * Math.PI * 2;
        initialX = centerX + Math.cos(angle) * 360;
        initialY = centerY + Math.sin(angle) * 210;
      } else {
        const angle = (i / Math.max(1, n)) * Math.PI * 2;
        const radius = 140 + (i % 4) * 45;
        initialX = centerX + Math.cos(angle) * radius;
        initialY = centerY + Math.sin(angle) * (radius * 0.7);
      }

      return {
        id: node.id,
        name: node.name,
        category: cat,
        raw: node,
        x: initialX,
        y: initialY,
        vx: 0,
        vy: 0,
        fx: isAff ? centerX : null,
        fy: isAff ? centerY : null,
        radius: isAff ? 28 : cat === 'gateway' || cat === 'datastore' ? 24 : 20,
        degree: totalDeg,
      };
    });

    const initialEdges: SimEdge[] = [];
    rawEdges.forEach((edge, idx) => {
      const src = getFrom(edge);
      const tgt = getTo(edge);
      if (src && tgt) {
        initialEdges.push({
          id: `edge-${idx}-${src}-${tgt}`,
          source: src,
          target: tgt,
          raw: edge,
        });
      }
    });

    simNodesRef.current = initialNodes;
    simEdgesRef.current = initialEdges;
    alphaRef.current = 1.0;
  }, [rawNodes, rawEdges, nodeCategoryMap, nodeDegreeMap, centerX, centerY, getFrom, getTo]);

  // Smooth, Stutter-Free Force Physics Simulation Loop
  useEffect(() => {
    let running = true;

    const simulateStep = (timestamp: number) => {
      if (!running) return;

      const alpha = alphaRef.current;
      const nodes = simNodesRef.current;
      const edges = simEdgesRef.current;

      if (alpha > 0.0015 && nodes.length > 0) {
        // 1. Softened Coulomb Many-Body Repulsion (with epsilon softening & clamping to prevent sudden explosive jumps)
        const kRep = 4200;
        const softening = 180; // prevents force explosion at near-zero distances
        const maxForce = 9.0;

        for (let i = 0; i < nodes.length; i++) {
          const n1 = nodes[i];
          for (let j = i + 1; j < nodes.length; j++) {
            const n2 = nodes[j];
            const dx = n2.x - n1.x;
            const dy = n2.y - n1.y;
            const distSq = dx * dx + dy * dy;

            if (distSq < 160000) { // cutoff at 400px
              const dist = Math.sqrt(distSq) + 0.1;
              let force = (kRep / (distSq + softening)) * alpha;
              if (force > maxForce) force = maxForce;

              const fx = (dx / dist) * force;
              const fy = (dy / dist) * force;
              n1.vx -= fx;
              n1.vy -= fy;
              n2.vx += fx;
              n2.vy += fy;
            }
          }
        }

        // 2. Hooke's Law Spring Tension along links
        const nodeMap = new Map<string, SimNode>();
        nodes.forEach((n) => nodeMap.set(n.id, n));

        const targetLinkDist = 120;
        const kSpring = 0.038;
        const maxSpringForce = 8.0;

        edges.forEach((edge) => {
          const s = nodeMap.get(edge.source);
          const t = nodeMap.get(edge.target);
          if (!s || !t) return;

          const dx = t.x - s.x;
          const dy = t.y - s.y;
          const dist = Math.sqrt(dx * dx + dy * dy) + 0.1;

          let springForce = (dist - targetLinkDist) * kSpring * alpha;
          if (springForce > maxSpringForce) springForce = maxSpringForce;
          if (springForce < -maxSpringForce) springForce = -maxSpringForce;

          const fx = (dx / dist) * springForce;
          const fy = (dy / dist) * springForce;

          s.vx += fx;
          s.vy += fy;
          t.vx -= fx;
          t.vy -= fy;
        });

        // 3. Center Gravity & Peripheral Orbit for Isolated Nodes
        const kCenter = 0.009;
        nodes.forEach((node) => {
          if (node.degree === 0) {
            // Isolated/standalone nodes gently orbit around outer perimeter instead of crowding center
            const dx = node.x - centerX;
            const dy = node.y - centerY;
            const dist = Math.sqrt(dx * dx + dy * dy) + 0.1;
            const targetOrbit = 340;
            const orbitForce = (dist - targetOrbit) * 0.006 * alpha;
            node.vx -= (dx / dist) * orbitForce;
            node.vy -= (dy / dist) * orbitForce;
          } else {
            // Connected nodes pulled towards center
            node.vx += (centerX - node.x) * kCenter * alpha;
            node.vy += (centerY - node.y) * kCenter * alpha;
          }
        });

        // 4. Collision Avoidance (gentle separation)
        for (let i = 0; i < nodes.length; i++) {
          const n1 = nodes[i];
          for (let j = i + 1; j < nodes.length; j++) {
            const n2 = nodes[j];
            const dx = n2.x - n1.x;
            const dy = n2.y - n1.y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            const minDist = n1.radius + n2.radius + 14;

            if (dist < minDist && dist > 0.001) {
              const overlap = (minDist - dist) * 0.35;
              const ox = (dx / dist) * overlap;
              const oy = (dy / dist) * overlap;
              n1.x -= ox;
              n1.y -= oy;
              n2.x += ox;
              n2.y += oy;
            }
          }
        }

        // 5. Velocity Damping & Position Integration (Smooth Euler)
        const damping = 0.85;
        nodes.forEach((node) => {
          if (node.fx !== null && node.fy !== null) {
            node.x = node.fx;
            node.y = node.fy;
            node.vx = 0;
            node.vy = 0;
          } else {
            node.vx *= damping;
            node.vy *= damping;
            node.x += node.vx;
            node.y += node.vy;

            // Soft boundary padding
            const pad = 35;
            if (node.x < pad) { node.x = pad; node.vx = 0; }
            if (node.x > width - pad) { node.x = width - pad; node.vx = 0; }
            if (node.y < pad) { node.y = pad; node.vy = 0; }
            if (node.y > height - pad) { node.y = height - pad; node.vy = 0; }
          }
        });

        // Smoothly cool down energy
        alphaRef.current *= 0.985;

        // Throttle frame updates to sync with screen refresh without GC stutter
        if (timestamp - lastTickTimeRef.current >= 15) {
          lastTickTimeRef.current = timestamp;
          setFrameTick((t) => (t + 1) % 1000000);
        }
      }

      animFrameRef.current = requestAnimationFrame(simulateStep);
    };

    animFrameRef.current = requestAnimationFrame(simulateStep);

    return () => {
      running = false;
      if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
    };
  }, [centerX, centerY, width, height]);

  // Wake up physics simulation gently
  const wakeSimulation = useCallback((energy: number = 0.3) => {
    alphaRef.current = Math.max(alphaRef.current, energy);
  }, []);

  // Compute active focus (hovered or clicked node)
  const activeFocusId = hoveredNodeId || selectedNodeId;

  // Direct neighbors for highlighting
  const activeDirectNeighbors = useMemo(() => {
    if (!activeFocusId) return new Set<string>();
    const neighbors = directNeighborMap.get(activeFocusId) || new Set<string>();
    const set = new Set<string>(neighbors);
    set.add(activeFocusId);
    return set;
  }, [activeFocusId, directNeighborMap]);

  const activeConnectedEdges = useMemo(() => {
    if (!activeFocusId) return new Set<string>();
    return activeEdgeMap.get(activeFocusId) || new Set<string>();
  }, [activeFocusId, activeEdgeMap]);

  // Exact standard SVG coordinate translation using getScreenCTM
  // Completely eliminates offset or shifting when clicking/dragging
  const getSvgCoordinates = useCallback(
    (clientX: number, clientY: number) => {
      if (!svgRef.current) return { x: 0, y: 0 };
      const svg = svgRef.current;
      const pt = svg.createSVGPoint();
      pt.x = clientX;
      pt.y = clientY;
      const ctm = svg.getScreenCTM();
      if (!ctm) return { x: 0, y: 0 };
      const svgPoint = pt.matrixTransform(ctm.inverse());

      // Adjust for pan and zoom transformation
      return {
        x: (svgPoint.x - pan.x) / zoom,
        y: (svgPoint.y - pan.y) / zoom,
      };
    },
    [pan.x, pan.y, zoom]
  );

  // Pointer event handlers for Dragging Nodes and Panning
  const handlePointerDown = (e: React.PointerEvent<SVGSVGElement>) => {
    const target = e.target as SVGElement;
    const nodeElem = target.closest('[data-node-id]');

    if (nodeElem) {
      const nodeId = nodeElem.getAttribute('data-node-id');
      if (nodeId) {
        e.stopPropagation();
        (e.target as Element).setPointerCapture(e.pointerId);
        setDraggedNodeId(nodeId);
        setSelectedNodeId(nodeId);

        const coords = getSvgCoordinates(e.clientX, e.clientY);
        const node = simNodesRef.current.find((n) => n.id === nodeId);
        if (node) {
          // Record drag offset so node center doesn't snap to cursor
          dragStartOffsetRef.current = {
            offsetX: coords.x - node.x,
            offsetY: coords.y - node.y,
          };
          node.fx = node.x;
          node.fy = node.y;
          wakeSimulation(0.35);
        }
        return;
      }
    }

    // Otherwise, start panning the background canvas
    setIsPanning(true);
    setPanStart({ x: e.clientX - pan.x, y: e.clientY - pan.y });
  };

  const handlePointerMove = (e: React.PointerEvent<SVGSVGElement>) => {
    if (draggedNodeId) {
      const coords = getSvgCoordinates(e.clientX, e.clientY);
      const node = simNodesRef.current.find((n) => n.id === draggedNodeId);
      if (node) {
        node.fx = coords.x - dragStartOffsetRef.current.offsetX;
        node.fy = coords.y - dragStartOffsetRef.current.offsetY;
        wakeSimulation(0.25);
      }
      return;
    }

    if (isPanning) {
      setPan({
        x: e.clientX - panStart.x,
        y: e.clientY - panStart.y,
      });
    }
  };

  const handlePointerUp = () => {
    if (draggedNodeId) {
      const node = simNodesRef.current.find((n) => n.id === draggedNodeId);
      if (node) {
        // Release fixed position so node floats gently in equilibrium
        node.fx = null;
        node.fy = null;
        wakeSimulation(0.2);
      }
      setDraggedNodeId(null);
    }
    setIsPanning(false);
  };

  // Wheel zoom handler
  const handleWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    e.preventDefault();
    const zoomFactor = e.deltaY < 0 ? 1.1 : 0.91;
    setZoom((prevZoom) => Math.min(Math.max(prevZoom * zoomFactor, 0.45), 2.8));
    wakeSimulation(0.1);
  };

  const resetView = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
    setSelectedNodeId(null);
    setHoveredNodeId(null);
    setSelectedCategory(null);
    wakeSimulation(0.6);
  };

  const reFloatGraph = () => {
    simNodesRef.current.forEach((n) => {
      n.vx += (Math.random() - 0.5) * 18;
      n.vy += (Math.random() - 0.5) * 18;
      n.fx = null;
      n.fy = null;
    });
    wakeSimulation(0.9);
  };

  if (loading) {
    return (
      <div className="glass-panel" style={{ padding: '32px', textAlign: 'center', color: '#94a3b8' }}>
        Rendering floating topology graph...
      </div>
    );
  }

  if (rawNodes.length === 0) {
    return (
      <div className="glass-panel" style={{ padding: '32px', textAlign: 'center', color: '#94a3b8' }}>
        No topology graph data available. Run topology init/refresh to populate.
      </div>
    );
  }

  // Active focused node details
  const focusedSimNode = simNodesRef.current.find((n) => n.id === activeFocusId);

  return (
    <div className="glass-panel" style={{ padding: '24px', borderRadius: '12px', marginBottom: '20px' }}>
      {/* Header & Controls Bar */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '14px', flexWrap: 'wrap', gap: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <Layers size={20} color="#38bdf8" />
          <div>
            <h3 style={{ fontSize: '1.05rem', fontWeight: 700, color: '#f8fafc', margin: 0 }}>
              Topology Graph ({rawNodes.length} Services, {rawEdges.length} Links)
            </h3>
            <p style={{ fontSize: '0.74rem', color: '#64748b', margin: '2px 0 0 0' }}>
              Obsidian-style floating graph • Drag nodes to reposition • Hover to illuminate direct neighbors
            </p>
          </div>
        </div>

        {/* Action Controls */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          {!hideNamespaceDropdown && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginRight: '6px' }}>
              <span style={{ fontSize: '0.75rem', color: '#94a3b8', fontWeight: 500 }}>Namespace:</span>
              <select
                value={selectedNamespace}
                onChange={(e) => {
                  const ns = e.target.value;
                  setSelectedNamespace(ns);
                  if (onNamespaceChange) {
                    onNamespaceChange(ns);
                  }
                }}
                className="btn btn-secondary"
                style={{
                  padding: '4px 10px',
                  fontSize: '0.78rem',
                  background: 'rgba(30, 41, 59, 0.85)',
                  color: '#38bdf8',
                  border: '1px solid rgba(56, 189, 248, 0.3)',
                  borderRadius: '6px',
                  cursor: observedNamespaces.length > 1 ? 'pointer' : 'default',
                  outline: 'none',
                  fontWeight: 600,
                }}
              >
                {observedNamespaces.map((ns) => (
                  <option key={ns} value={ns}>
                    {ns} (Observing)
                  </option>
                ))}
              </select>
            </div>
          )}
          <button
            onClick={() => setZoom((z) => Math.min(z * 1.2, 2.8))}
            className="btn btn-secondary"
            style={{ padding: '4px 10px', fontSize: '0.8rem', fontWeight: 600 }}
            title="Zoom In"
          >
            +
          </button>
          <button
            onClick={() => setZoom((z) => Math.max(z * 0.83, 0.45))}
            className="btn btn-secondary"
            style={{ padding: '4px 10px', fontSize: '0.8rem', fontWeight: 600 }}
            title="Zoom Out"
          >
            −
          </button>
          <button
            onClick={resetView}
            className="btn btn-secondary"
            style={{ padding: '4px 10px', fontSize: '0.75rem' }}
            title="Reset Pan and Zoom"
          >
            Reset
          </button>
          <button
            onClick={reFloatGraph}
            className="btn btn-secondary"
            style={{ padding: '4px 10px', fontSize: '0.75rem', display: 'flex', alignItems: 'center', gap: '5px' }}
            title="Re-float graph dynamics"
          >
            <RefreshCw size={13} />
            Re-Float
          </button>
        </div>
      </div>

      {/* Multi-Type Comprehensive Legend Bar */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        flexWrap: 'wrap',
        marginBottom: '14px',
        padding: '8px 12px',
        backgroundColor: 'rgba(15, 23, 42, 0.65)',
        borderRadius: '8px',
        border: '1px solid var(--border-subtle)',
        fontSize: '0.73rem',
      }}>
        <span style={{ fontWeight: 600, color: '#94a3b8', marginRight: '4px' }}>Legend:</span>
        {(Object.keys(CATEGORY_STYLES) as NodeCategory[]).map((cat) => {
          const style = CATEGORY_STYLES[cat];
          const count = categoryCounts[cat] || 0;

          // Only show 'affected' filter when viewing an incident with an affected service
          if (cat === 'affected' && !affectedService) return null;
          if (count === 0) return null;

          const isFilterActive = selectedCategory === cat;

          return (
            <button
              key={cat}
              onClick={() => {
                setSelectedCategory(isFilterActive ? null : cat);
                wakeSimulation(0.25);
              }}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '3px 8px',
                borderRadius: '6px',
                border: `1px solid ${isFilterActive ? style.color : 'transparent'}`,
                backgroundColor: isFilterActive ? style.bg : 'rgba(255, 255, 255, 0.04)',
                color: isFilterActive ? style.color : '#cbd5e1',
                cursor: 'pointer',
                transition: 'all 0.15s ease',
              }}
            >
              <span
                style={{
                  width: '9px',
                  height: '9px',
                  borderRadius: '50%',
                  backgroundColor: style.color,
                  boxShadow: `0 0 6px ${style.color}`,
                }}
              />
              <span style={{ fontWeight: 600 }}>{style.label}</span>
              <span style={{ fontSize: '0.68rem', opacity: 0.75, fontFamily: 'var(--font-mono)' }}>({count})</span>
            </button>
          );
        })}
      </div>

      {/* Interactive Floating Graph Viewport */}
      <div
        ref={containerRef}
        onWheel={handleWheel}
        style={{
          position: 'relative',
          backgroundColor: '#07090e',
          borderRadius: '10px',
          border: '1px solid var(--border-subtle)',
          overflow: 'hidden',
          cursor: isPanning ? 'grabbing' : draggedNodeId ? 'grabbing' : 'grab',
          userSelect: 'none',
        }}
      >
        <svg
          ref={svgRef}
          viewBox={`0 0 ${width} ${height}`}
          style={{ width: '100%', height: '540px', display: 'block' }}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerLeave={handlePointerUp}
        >
          <defs>
            {/* Directional markers calibrated to land exactly on node perimeter */}
            <marker id="arrow-default" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
              <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="#38bdf8" opacity="0.65" />
            </marker>
            <marker id="arrow-highlight" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
              <path d="M 0 1.5 L 9 5 L 0 8.5 z" fill="#38bdf8" />
            </marker>
            <marker id="arrow-faded" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
              <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="#475569" opacity="0.25" />
            </marker>

            {/* Radial glow filter */}
            <filter id="node-glow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur in="SourceGraphic" stdDeviation="3.5" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>

          {/* Background grid dots for spatial orientation */}
          <g transform={`translate(${pan.x % 40}, ${pan.y % 40})`} opacity="0.07">
            {Array.from({ length: 28 }).map((_, r) =>
              Array.from({ length: 48 }).map((_, c) => (
                <circle key={`dot-${r}-${c}`} cx={c * 40} cy={r * 40} r="1" fill="#94a3b8" />
              ))
            )}
          </g>

          {/* Root Transformed Group (Pan & Zoom) */}
          <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
            {/* Render Links / Edges - Trimmed exactly to node circle boundaries! */}
            {simEdgesRef.current.map((edge) => {
              const nodes = simNodesRef.current;
              const sourceNode = nodes.find((n) => n.id === edge.source);
              const targetNode = nodes.find((n) => n.id === edge.target);
              if (!sourceNode || !targetNode) return null;

              const isEdgeHighlighted = activeFocusId
                ? activeConnectedEdges.has(edge.id)
                : selectedCategory
                ? sourceNode.category === selectedCategory || targetNode.category === selectedCategory
                : false;

              const isEdgeDimmed = activeFocusId
                ? !isEdgeHighlighted
                : selectedCategory
                ? !isEdgeHighlighted
                : false;

              // Calculate boundary start and end points (instead of drawing from centers)
              const dx = targetNode.x - sourceNode.x;
              const dy = targetNode.y - sourceNode.y;
              const dist = Math.sqrt(dx * dx + dy * dy);

              // Skip rendering if nodes are overlapping
              if (dist <= sourceNode.radius + targetNode.radius + 6) return null;

              const ux = dx / dist;
              const uy = dy / dist;

              // Start exactly at source perimeter
              const startX = sourceNode.x + ux * sourceNode.radius;
              const startY = sourceNode.y + uy * sourceNode.radius;

              // End exactly at target perimeter leaving room for arrowhead (offset by 4px)
              const arrowOffset = isEdgeHighlighted ? 5 : 4;
              const endX = targetNode.x - ux * (targetNode.radius + arrowOffset);
              const endY = targetNode.y - uy * (targetNode.radius + arrowOffset);

              const srcStyle = CATEGORY_STYLES[sourceNode.category];

              return (
                <line
                  key={edge.id}
                  x1={startX}
                  y1={startY}
                  x2={endX}
                  y2={endY}
                  stroke={isEdgeHighlighted ? srcStyle.color : isEdgeDimmed ? 'rgba(71, 85, 105, 0.12)' : 'rgba(56, 189, 248, 0.28)'}
                  strokeWidth={isEdgeHighlighted ? 2.2 : isEdgeDimmed ? 0.8 : 1.2}
                  strokeDasharray={isEdgeDimmed ? '3 3' : undefined}
                  opacity={isEdgeHighlighted ? 1 : isEdgeDimmed ? 0.15 : 0.6}
                  markerEnd={isEdgeHighlighted ? 'url(#arrow-highlight)' : isEdgeDimmed ? 'url(#arrow-faded)' : 'url(#arrow-default)'}
                />
              );
            })}

            {/* Render Floating Nodes */}
            {simNodesRef.current.map((node) => {
              const style = CATEGORY_STYLES[node.category];
              const isAffected = node.category === 'affected';
              const isFocused = activeFocusId === node.id;
              const isDirectNeighbor = activeFocusId ? activeDirectNeighbors.has(node.id) : true;
              const isCategoryMatch = selectedCategory ? node.category === selectedCategory : true;

              const isDimmed = (activeFocusId && !isDirectNeighbor) || (selectedCategory && !isCategoryMatch);
              const nodeOpacity = isFocused ? 1 : isDimmed ? 0.14 : 0.95;

              return (
                <g
                  key={node.id}
                  data-node-id={node.id}
                  transform={`translate(${node.x}, ${node.y})`}
                  style={{ cursor: 'pointer' }}
                  opacity={nodeOpacity}
                  onMouseEnter={() => setHoveredNodeId(node.id)}
                  onMouseLeave={() => setHoveredNodeId(null)}
                  onClick={(e) => {
                    e.stopPropagation();
                    setSelectedNodeId((prev) => (prev === node.id ? null : node.id));
                  }}
                >
                  {/* Outer pulsating ring for affected root cause */}
                  {isAffected && (
                    <circle
                      r={node.radius + 12}
                      fill="none"
                      stroke={style.color}
                      strokeWidth="1.8"
                      strokeDasharray="4 3"
                      opacity="0.75"
                      className="pulse"
                    />
                  )}

                  {/* Active focus outer halo */}
                  {(isFocused || isAffected) && (
                    <circle
                      r={node.radius + 6}
                      fill="none"
                      stroke={style.color}
                      strokeWidth="2"
                      opacity={isFocused ? 0.95 : 0.4}
                      filter="url(#node-glow)"
                    />
                  )}

                  {/* Node Body */}
                  <circle
                    r={isFocused ? node.radius + 3 : node.radius}
                    fill={isFocused ? style.bg : 'rgba(10, 14, 23, 0.95)'}
                    stroke={style.color}
                    strokeWidth={isFocused ? 2.8 : 1.8}
                  />

                  {/* Node Label Text */}
                  <text
                    textAnchor="middle"
                    dy="-2"
                    fill="#ffffff"
                    fontSize={isFocused ? '10.5px' : '9px'}
                    fontWeight={isFocused || isAffected ? '700' : '600'}
                    fontFamily="var(--font-sans)"
                    pointerEvents="none"
                  >
                    {node.name.length > 14 ? `${node.name.slice(0, 12)}…` : node.name}
                  </text>

                  {/* Port / Type Sub-label */}
                  <text
                    textAnchor="middle"
                    dy="10"
                    fill={isFocused ? style.color : '#94a3b8'}
                    fontSize="7.5px"
                    fontFamily="var(--font-mono)"
                    pointerEvents="none"
                  >
                    {node.raw.ports && node.raw.ports.length > 0
                      ? `:${node.raw.ports[0]}`
                      : node.raw.kind || style.label.split(' ')[0]}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>

        {/* Floating Detail Overlay on Node Focus */}
        {focusedSimNode && (
          <div
            style={{
              position: 'absolute',
              bottom: '16px',
              left: '16px',
              padding: '12px 16px',
              backgroundColor: 'rgba(15, 23, 42, 0.92)',
              backdropFilter: 'blur(10px)',
              borderRadius: '8px',
              border: `1px solid ${CATEGORY_STYLES[focusedSimNode.category].color}`,
              boxShadow: `0 8px 24px rgba(0, 0, 0, 0.6), 0 0 12px ${CATEGORY_STYLES[focusedSimNode.category].glow}`,
              fontSize: '0.75rem',
              color: '#f8fafc',
              zIndex: 10,
              maxWidth: '320px',
              pointerEvents: 'none',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '6px' }}>
              <span style={{ fontWeight: 700, fontSize: '0.88rem', color: CATEGORY_STYLES[focusedSimNode.category].color }}>
                {focusedSimNode.name}
              </span>
              <span style={{
                fontSize: '0.65rem',
                padding: '2px 6px',
                borderRadius: '4px',
                backgroundColor: CATEGORY_STYLES[focusedSimNode.category].bg,
                border: `1px solid ${CATEGORY_STYLES[focusedSimNode.category].border}`,
                color: CATEGORY_STYLES[focusedSimNode.category].color,
                fontWeight: 600,
              }}>
                {CATEGORY_STYLES[focusedSimNode.category].label}
              </span>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', fontSize: '0.7rem', color: '#94a3b8' }}>
              <div>
                <span style={{ color: '#64748b' }}>Namespace: </span>
                <span style={{ color: '#e2e8f0', fontFamily: 'var(--font-mono)' }}>{focusedSimNode.raw.namespace || 'default'}</span>
              </div>
              <div>
                <span style={{ color: '#64748b' }}>Ports: </span>
                <span style={{ color: '#e2e8f0', fontFamily: 'var(--font-mono)' }}>
                  {focusedSimNode.raw.ports && focusedSimNode.raw.ports.length > 0 ? focusedSimNode.raw.ports.join(', ') : 'None'}
                </span>
              </div>
              <div>
                <span style={{ color: '#64748b' }}>In-Degree: </span>
                <span style={{ color: '#38bdf8', fontWeight: 600, fontFamily: 'var(--font-mono)' }}>
                  {nodeDegreeMap.inDeg[focusedSimNode.id] || 0}
                </span>
              </div>
              <div>
                <span style={{ color: '#64748b' }}>Out-Degree: </span>
                <span style={{ color: '#38bdf8', fontWeight: 600, fontFamily: 'var(--font-mono)' }}>
                  {nodeDegreeMap.outDeg[focusedSimNode.id] || 0}
                </span>
              </div>
            </div>

            {focusedSimNode.raw.resource_limits && (
              <div style={{ marginTop: '6px', borderTop: '1px solid rgba(255,255,255,0.08)', paddingTop: '6px', fontSize: '0.68rem', color: '#94a3b8' }}>
                <span style={{ color: '#64748b' }}>Limits: </span>
                <span style={{ color: '#cbd5e1', fontFamily: 'var(--font-mono)' }}>
                  {focusedSimNode.raw.resource_limits.cpu ? `CPU: ${focusedSimNode.raw.resource_limits.cpu} ` : ''}
                  {focusedSimNode.raw.resource_limits.memory ? `Mem: ${focusedSimNode.raw.resource_limits.memory}` : ''}
                </span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};
