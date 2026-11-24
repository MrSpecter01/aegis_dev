"""Dynamic Kubernetes cluster topology discovery and drift detection.

Discovers Deployments, Services, and container specifications across monitored namespaces
and builds TopologyNode / TopologyEdge structures in the TopologyStore.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse
import httpx

from shared.models.topology import TopologyNode, TopologyEdge
from services.api_server.topology.store import TopologyStore
from services.mcp.tools.kubernetes import resolve_k8s_config

logger = logging.getLogger("aegis.topology.discovery")

_IGNORED_RUNTIME_SERVICES = {
    "prometheus", "jaeger", "tempo", "loki", "grafana", "alertmanager",
    "otel-collector", "otel-collector-agent", "opentelemetry-collector",
    "kube-state-metrics",
}


def _extract_host_candidates_from_env(
    env_name: str,
    env_val: str,
    current_name: str,
) -> Set[str]:
    """Extract target service hostnames from URLs, host:port pairs, or address env vars."""
    candidates: Set[str] = set()
    if not env_val:
        return candidates

    dep_key_markers = ("_ADDR", "_HOST", "_URL", "_SERVICE", "_ENDPOINT", "BROKERS")
    looks_like_endpoint = (
        "://" in env_val
        or ":" in env_val
        or any(marker in env_name.upper() for marker in dep_key_markers)
    )
    if not looks_like_endpoint:
        return candidates

    for token in re.split(r"[,;\s]+", env_val.strip()):
        if not token:
            continue
        host = ""
        if "://" in token:
            parsed = urlparse(token)
            host = parsed.hostname or ""
        else:
            host = token.split("/")[0].split(":")[0].strip()

        if not host:
            continue
        host_short = host.split(".")[0].strip()
        if host_short and host_short not in (current_name, "localhost", "127.0.0.1", "0.0.0.0"):
            if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", host_short):
                candidates.add(host_short)

    return candidates


def _match_known_service(raw_name: str, known_by_name: Dict[str, TopologyNode]) -> Optional[str]:
    """Map raw trace/gRPC/HTTP target string to a known service node ID via exact or normalized match."""
    if not raw_name:
        return None
    cleaned = raw_name.strip().lower().split(".")[0]
    if cleaned in _IGNORED_RUNTIME_SERVICES:
        return None
    if cleaned in known_by_name:
        return known_by_name[cleaned].id

    # Match CamelCase/gRPC service suffixes such as "CartService" -> "cart" or "cartservice"
    norm = re.sub(r"[-_]", "", cleaned)
    for k_name, node in known_by_name.items():
        k_norm = re.sub(r"[-_]", "", k_name.lower())
        if norm == k_norm or norm == f"{k_norm}service" or f"{norm}service" == k_norm:
            return node.id
    return None


def _extract_target_from_span_name(span_name: str, span_tags: Dict[str, Any], known_by_name: Dict[str, TopologyNode]) -> Optional[str]:
    """Extract downstream target service node ID from span tags or gRPC/HTTP span names."""
    for tag_key in ("peer.service", "server.address", "net.peer.name", "rpc.service", "http.host"):
        val = span_tags.get(tag_key)
        if val:
            matched = _match_known_service(str(val).split(":")[0], known_by_name)
            if matched:
                return matched

    if span_name:
        # e.g. "oteldemo.CartService/EmptyCart" or "hipstershop.ProductCatalogService/ListProducts"
        grpc_match = re.match(r"^(?:[a-zA-Z0-9_]+\.)?([a-zA-Z0-9_-]+)/", span_name.strip())
        if grpc_match:
            matched = _match_known_service(grpc_match.group(1), known_by_name)
            if matched:
                return matched

        # Match URL or bare known service token inside span_name
        url_match = re.search(r"://([a-zA-Z0-9_.-]+)", span_name)
        if url_match:
            matched = _match_known_service(url_match.group(1), known_by_name)
            if matched:
                return matched

    return None


async def discover_cluster_topology(
    namespaces: List[str],
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[List[TopologyNode], List[TopologyEdge]]:
    """
    Query Kubernetes API to discover Deployments, StatefulSets, DaemonSets, and Services
    in the given namespaces. Extracts replica counts, resource limits, ports, and dependencies.
    """
    server, headers, verify_ssl = resolve_k8s_config()
    if not server:
        logger.warning("[TOPOLOGY DISCOVERY] Kubernetes server URL could not be resolved; skipping discovery")
        return [], []

    nodes: List[TopologyNode] = []
    edges: List[TopologyEdge] = []
    service_selector_map: Dict[str, Dict[str, str]] = {}
    service_port_map: Dict[str, int] = {}

    workload_endpoints = [
        ("deployments", "Deployment"),
        ("statefulsets", "StatefulSet"),
        ("daemonsets", "DaemonSet"),
    ]

    async def _fetch(c: httpx.AsyncClient) -> Tuple[List[TopologyNode], List[TopologyEdge]]:
        for ns in namespaces:
            ns = ns.strip()
            if not ns:
                continue

            # 1. Fetch Services in namespace
            svc_url = f"{server}/api/v1/namespaces/{ns}/services"
            try:
                resp = await c.get(svc_url, headers=headers, timeout=5.0)
                if resp.status_code == 200:
                    svc_items = resp.json().get("items", [])
                    for item in svc_items:
                        s_name = item.get("metadata", {}).get("name", "")
                        spec = item.get("spec", {})
                        selector = spec.get("selector", {})
                        if s_name and selector:
                            service_selector_map[f"{s_name}.{ns}"] = selector
                        ports = spec.get("ports", [])
                        if ports and "port" in ports[0]:
                            service_port_map[f"{s_name}.{ns}"] = ports[0]["port"]
            except Exception as exc:
                logger.debug("[TOPOLOGY DISCOVERY] Failed fetching services in %s: %s", ns, exc)

            # 2. Fetch Deployments, StatefulSets, and DaemonSets in namespace
            for endpoint_name, kind_label in workload_endpoints:
                wk_url = f"{server}/apis/apps/v1/namespaces/{ns}/{endpoint_name}"
                try:
                    resp = await c.get(wk_url, headers=headers, timeout=5.0)
                    if resp.status_code != 200:
                        continue
                    items = resp.json().get("items", [])
                    for item in items:
                        w_name = item.get("metadata", {}).get("name", "")
                        if not w_name:
                            continue
                        node_id = f"{w_name}.{ns}"

                        spec = item.get("spec", {})
                        if kind_label == "DaemonSet":
                            replicas = item.get("status", {}).get("desiredNumberScheduled", 1) or 1
                        else:
                            replicas = spec.get("replicas", 1)
                            if replicas is None:
                                replicas = 1
                        template = spec.get("template", {})
                        pod_spec = template.get("spec", {})
                        labels = template.get("metadata", {}).get("labels", {})

                        resource_limits: Dict[str, str] = {}
                        ports: List[int] = []
                        env_deps: Set[str] = set()

                        for container in pod_spec.get("containers", []):
                            limits = container.get("resources", {}).get("limits", {})
                            for k, v in limits.items():
                                resource_limits[str(k)] = str(v)

                            for p in container.get("ports", []):
                                cp = p.get("containerPort")
                                if cp and isinstance(cp, int) and cp not in ports:
                                    ports.append(cp)

                            for env_var in container.get("env", []):
                                e_name = str(env_var.get("name", ""))
                                e_val = str(env_var.get("value", ""))
                                env_deps.update(_extract_host_candidates_from_env(e_name, e_val, w_name))

                        nodes.append(
                            TopologyNode(
                                id=node_id,
                                name=w_name,
                                namespace=ns,
                                type=kind_label,
                                replicas=replicas,
                                resource_limits=resource_limits,
                                ports=ports,
                                labels=labels,
                                dependencies=sorted(env_deps),
                                stub=False,
                            )
                        )
                except Exception as exc:
                    logger.debug("[TOPOLOGY DISCOVERY] Failed fetching %s in %s: %s", endpoint_name, ns, exc)

        # 3. Derive TopologyEdges from service selectors and explicit env dependencies
        node_name_lookup = {n.name: n for n in nodes}

        for node in nodes:
            for dep_name in node.dependencies:
                target_node = node_name_lookup.get(dep_name)
                if target_node and target_node.id != node.id:
                    edges.append(
                        TopologyEdge(
                            from_node=node.id,
                            to_node=target_node.id,
                            protocol="TCP",
                            port=target_node.ports[0] if target_node.ports else 80,
                        )
                    )

            for svc_id, selector in service_selector_map.items():
                svc_ns = svc_id.split(".")[-1]
                if node.namespace == svc_ns:
                    matches = all(node.labels.get(k) == v for k, v in selector.items())
                    if matches:
                        svc_short_name = svc_id.split(".")[0]
                        for other_node in nodes:
                            if other_node.id != node.id and svc_short_name in other_node.dependencies:
                                edges.append(
                                    TopologyEdge(
                                        from_node=other_node.id,
                                        to_node=node.id,
                                        protocol="TCP",
                                        port=service_port_map.get(svc_id, 80),
                                    )
                                )

        unique_edges: List[TopologyEdge] = []
        seen_edges: Set[Tuple[str, str]] = set()
        for e in edges:
            pair = (e.from_node, e.to_node)
            if pair not in seen_edges:
                seen_edges.add(pair)
                unique_edges.append(e)

        return nodes, unique_edges

    if client:
        return await _fetch(client)
    async with httpx.AsyncClient(verify=verify_ssl) as new_client:
        return await _fetch(new_client)


async def _probe_jaeger_runtime_edges(
    c: httpx.AsyncClient,
    jaeger_url: str,
    default_ns: str,
    known_by_name: Dict[str, TopologyNode],
    ensure_node: Any,
    add_edge: Any,
) -> None:
    """Optional Jaeger trace backend probe for runtime service edges."""
    if not jaeger_url:
        return
    try:
        dep_resp = await c.get(f"{jaeger_url}/api/dependencies?lookback=3600000", timeout=4.0)
        if dep_resp.status_code == 200:
            for dep in dep_resp.json().get("data", []):
                parent_id = ensure_node(dep.get("parent", ""))
                child_id = ensure_node(dep.get("child", ""))
                add_edge(parent_id, child_id)
    except Exception as exc:
        logger.debug("[RUNTIME TOPOLOGY] Jaeger /api/dependencies skipped: %s", exc)

    for svc_name in list(known_by_name.keys())[:10]:
        try:
            tr_resp = await c.get(
                f"{jaeger_url}/api/traces",
                params={"service": svc_name, "limit": 5, "lookback": "1h"},
                timeout=4.0,
            )
            if tr_resp.status_code != 200:
                continue
            for trace in tr_resp.json().get("data", []):
                processes = trace.get("processes", {})
                spans = trace.get("spans", [])
                span_svc: Dict[str, str] = {}
                for sp in spans:
                    proc = processes.get(sp.get("processID", ""), {})
                    p_svc = proc.get("serviceName", "")
                    p_tags = {t.get("key"): t.get("value") for t in proc.get("tags", []) if isinstance(t, dict)}
                    p_ns = str(p_tags.get("k8s.namespace.name") or default_ns)
                    resolved = ensure_node(p_svc, p_ns)
                    if resolved:
                        span_svc[sp.get("spanID", "")] = resolved

                for sp in spans:
                    cur_id = span_svc.get(sp.get("spanID", ""))
                    if not cur_id:
                        continue
                    for ref in sp.get("references", []):
                        if ref.get("refType") in ("CHILD_OF", "FOLLOWS_FROM"):
                            parent_id = span_svc.get(ref.get("spanID", ""))
                            add_edge(parent_id, cur_id)
                    tags_map = {t.get("key"): t.get("value") for t in sp.get("tags", []) if isinstance(t, dict)}
                    target_id = _extract_target_from_span_name(sp.get("operationName", ""), tags_map, known_by_name)
                    add_edge(cur_id, target_id)
        except Exception as exc:
            logger.debug("[RUNTIME TOPOLOGY] Jaeger trace scan failed for %s: %s", svc_name, exc)


async def _probe_tempo_runtime_edges(
    c: httpx.AsyncClient,
    tempo_url: str,
    default_ns: str,
    known_by_name: Dict[str, TopologyNode],
    ensure_node: Any,
    add_edge: Any,
) -> None:
    """Optional Tempo OTLP trace backend probe for runtime service edges."""
    if not tempo_url:
        return
    try:
        search_resp = await c.get(f"{tempo_url}/api/search", params={"limit": 10}, timeout=4.0)
        if search_resp.status_code != 200:
            return
        for t_summary in search_resp.json().get("traces", []):
            tid = t_summary.get("traceID")
            if not tid:
                continue
            t_resp = await c.get(f"{tempo_url}/api/traces/{tid}", timeout=4.0)
            if t_resp.status_code != 200:
                continue
            span_svc_map: Dict[str, str] = {}
            all_otlp_spans: List[Tuple[str, Dict[str, Any]]] = []
            for batch in t_resp.json().get("batches", []):
                r_attrs = {
                    a.get("key"): next(iter(a.get("value", {}).values()), "")
                    for a in batch.get("resource", {}).get("attributes", [])
                    if isinstance(a, dict)
                }
                svc_name = str(r_attrs.get("service.name", ""))
                svc_ns = str(r_attrs.get("k8s.namespace.name") or default_ns)
                resolved = ensure_node(svc_name, svc_ns)
                if not resolved:
                    continue
                for scope in batch.get("scopeSpans", []):
                    for sp in scope.get("spans", []):
                        sid = sp.get("spanId", "")
                        if sid:
                            span_svc_map[sid] = resolved
                        all_otlp_spans.append((resolved, sp))
            for cur_id, sp in all_otlp_spans:
                parent_sid = sp.get("parentSpanId", "")
                if parent_sid and parent_sid in span_svc_map:
                    add_edge(span_svc_map[parent_sid], cur_id)
                tags_map = {
                    a.get("key"): next(iter(a.get("value", {}).values()), "")
                    for a in sp.get("attributes", [])
                    if isinstance(a, dict)
                }
                target_id = _extract_target_from_span_name(sp.get("name", ""), tags_map, known_by_name)
                add_edge(cur_id, target_id)
    except Exception as exc:
        logger.debug("[RUNTIME TOPOLOGY] Tempo trace scan skipped: %s", exc)


async def _probe_prometheus_runtime_edges(
    c: httpx.AsyncClient,
    prometheus_url: str,
    default_ns: str,
    ns_set: Set[str],
    known_by_name: Dict[str, TopologyNode],
    ensure_node: Any,
    add_edge: Any,
) -> None:
    """Optional Prometheus service-graph & span-metrics probe for runtime service edges."""
    if not prometheus_url:
        return
    for prom_expr, mode in (
        ("traces_service_graph_request_total", "service_graph"),
        ('traces_span_metrics_calls_total{span_kind="SPAN_KIND_CLIENT"}', "span_client"),
    ):
        try:
            p_resp = await c.get(f"{prometheus_url}/api/v1/query", params={"query": prom_expr}, timeout=4.0)
            if p_resp.status_code != 200:
                continue
            for item in p_resp.json().get("data", {}).get("result", []):
                m = item.get("metric", {})
                if mode == "service_graph":
                    c_ns = m.get("client_k8s_namespace_name") or m.get("namespace")
                    s_ns = m.get("server_k8s_namespace_name") or m.get("namespace")
                    if (c_ns and c_ns not in ns_set) or (s_ns and s_ns not in ns_set):
                        continue
                    src_id = ensure_node(m.get("client", ""), c_ns or default_ns)
                    dst_id = ensure_node(m.get("server", ""), s_ns or default_ns)
                    add_edge(src_id, dst_id)
                else:
                    m_ns = m.get("k8s_namespace_name") or m.get("namespace")
                    if m_ns and m_ns not in ns_set:
                        continue
                    src_id = ensure_node(m.get("service_name", ""), m_ns or default_ns)
                    dst_id = _extract_target_from_span_name(m.get("span_name", ""), m, known_by_name)
                    add_edge(src_id, dst_id)
        except Exception as exc:
            logger.debug("[RUNTIME TOPOLOGY] Prometheus span query '%s' skipped: %s", prom_expr, exc)


RUNTIME_TOPOLOGY_PROBES = (
    ("jaeger_url", "jaeger", _probe_jaeger_runtime_edges),
    ("tempo_url", "tempo", _probe_tempo_runtime_edges),
    ("prometheus_url", "prometheus", _probe_prometheus_runtime_edges),
)


async def discover_runtime_trace_topology(
    namespaces: List[str],
    mcp_endpoints: Optional[Any] = None,
    existing_nodes: Optional[List[TopologyNode]] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[List[TopologyNode], List[TopologyEdge]]:
    """Modular runtime topology enrichment across whichever trace/metric backends are active."""
    default_ns = namespaces[0].strip() if namespaces and namespaces[0].strip() else "default"
    ns_set = {ns.strip() for ns in namespaces if ns.strip()}

    runtime_nodes: Dict[str, TopologyNode] = {n.id: n.model_copy() for n in (existing_nodes or [])}
    known_by_name: Dict[str, TopologyNode] = {n.name.lower(): n for n in runtime_nodes.values()}
    edge_pairs: Set[Tuple[str, str]] = set()
    runtime_edges: List[TopologyEdge] = []

    def _ensure_node(raw_service: str, explicit_ns: Optional[str] = None) -> Optional[str]:
        if not raw_service:
            return None
        short = raw_service.strip().split(".")[0]
        if short.lower() in _IGNORED_RUNTIME_SERVICES:
            return None
        matched_id = _match_known_service(short, known_by_name)
        if matched_id:
            return matched_id
        if explicit_ns and explicit_ns in ns_set:
            nid = f"{short}.{explicit_ns}"
            if nid not in runtime_nodes:
                node = TopologyNode(
                    id=nid,
                    name=short,
                    namespace=explicit_ns,
                    type="Service",
                    replicas=1,
                    dependencies=[],
                    stub=False,
                )
                runtime_nodes[nid] = node
                known_by_name[short.lower()] = node
            return nid
        return None

    def _add_edge(src_id: Optional[str], dst_id: Optional[str]) -> None:
        if not src_id or not dst_id or src_id == dst_id:
            return
        if (src_id, dst_id) in edge_pairs:
            return
        edge_pairs.add((src_id, dst_id))
        dst_node = runtime_nodes.get(dst_id)
        port = dst_node.ports[0] if (dst_node and dst_node.ports) else None
        runtime_edges.append(
            TopologyEdge(from_node=src_id, to_node=dst_id, protocol="TCP", port=port)
        )
        src_node = runtime_nodes.get(src_id)
        if src_node and dst_id not in src_node.dependencies:
            src_node.dependencies.append(dst_id)

    def _ep(attr_key: str, dict_key: str) -> str:
        if isinstance(mcp_endpoints, dict):
            val = mcp_endpoints.get(attr_key) or mcp_endpoints.get(dict_key) or ""
        else:
            val = getattr(mcp_endpoints, attr_key, "") or getattr(mcp_endpoints, dict_key, "") or ""
        return str(val).rstrip("/")

    async def _discover(c: httpx.AsyncClient) -> None:
        for attr_key, dict_key, probe_fn in RUNTIME_TOPOLOGY_PROBES:
            endpoint_url = _ep(attr_key, dict_key)
            if not endpoint_url:
                continue
            if probe_fn is _probe_prometheus_runtime_edges:
                await probe_fn(c, endpoint_url, default_ns, ns_set, known_by_name, _ensure_node, _add_edge)
            else:
                await probe_fn(c, endpoint_url, default_ns, known_by_name, _ensure_node, _add_edge)

    if client:
        await _discover(client)
    else:
        async with httpx.AsyncClient() as new_client:
            await _discover(new_client)

    return list(runtime_nodes.values()), runtime_edges


async def check_topology_drift(
    store: TopologyStore,
    namespaces: List[str],
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """
    Compare stored TopologyNode specifications with live Kubernetes workloads.
    Returns a list of detected drift records and logs actionable warnings.
    """
    drifts: List[Dict[str, Any]] = []
    live_nodes, _ = await discover_cluster_topology(namespaces, client=client)
    live_lookup = {n.id: n for n in live_nodes}
    stored_nodes = store.all_nodes()
    stored_lookup = {n.id: n for n in stored_nodes}

    # 1. Check existing stored nodes against live cluster
    for stored_id, s_node in stored_lookup.items():
        if stored_id not in live_lookup:
            drift = {
                "node_id": stored_id,
                "type": "WORKLOAD_MISSING_IN_CLUSTER",
                "message": f"Workload '{stored_id}' exists in TopologyStore but was not found in cluster namespace.",
            }
            drifts.append(drift)
            logger.warning("[TOPOLOGY DRIFT] %s", drift["message"])
            continue

        l_node = live_lookup[stored_id]

        # Replica drift
        if s_node.replicas != l_node.replicas:
            drift = {
                "node_id": stored_id,
                "type": "REPLICA_COUNT_DRIFT",
                "stored_replicas": s_node.replicas,
                "live_replicas": l_node.replicas,
                "message": f"Workload '{stored_id}' replica drift: stored={s_node.replicas}, live={l_node.replicas}",
            }
            drifts.append(drift)
            logger.warning("[TOPOLOGY DRIFT] %s", drift["message"])

        # Resource limits drift
        if s_node.resource_limits != l_node.resource_limits:
            drift = {
                "node_id": stored_id,
                "type": "RESOURCE_LIMITS_DRIFT",
                "stored_limits": s_node.resource_limits,
                "live_limits": l_node.resource_limits,
                "message": f"Workload '{stored_id}' resource limits drift: stored={s_node.resource_limits}, live={l_node.resource_limits}",
            }
            drifts.append(drift)
            logger.warning("[TOPOLOGY DRIFT] %s", drift["message"])

    # 2. Check for newly discovered live workloads not in store
    for live_id, l_node in live_lookup.items():
        if live_id not in stored_lookup:
            drift = {
                "node_id": live_id,
                "type": "NEW_UNTRACKED_WORKLOAD",
                "message": f"Live workload '{live_id}' detected in cluster but missing from TopologyStore.",
            }
            drifts.append(drift)
            logger.info("[TOPOLOGY DISCOVERY] Found new workload in cluster: %s", live_id)

    return drifts

