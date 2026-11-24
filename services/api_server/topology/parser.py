import logging
import os
import re
from typing import Dict, List, Set, Tuple, Any, Optional
import yaml

from shared.models.topology import TopologyNode, TopologyEdge

logger = logging.getLogger("aegis.topology.parser")


def _clean_helm_template_text(content: str) -> str:
    """Strip or normalize Helm template directives {{ ... }} if they break YAML parsing."""
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("{{") and stripped.endswith("}}") and ":" not in stripped:
            continue
        lines.append(line)
    return re.sub(r"\{\{.*?\}\}", "template_placeholder", "\n".join(lines))


def _infer_manifest_name(
    metadata: Dict[str, Any],
    labels: Dict[str, Any],
    file_name: str,
) -> Optional[str]:
    """Resolve workload/service name from metadata, labels, or template filename."""
    raw_name = str(metadata.get("name") or "")
    if raw_name and "template_placeholder" not in raw_name:
        return raw_name
    for label_key in ("app.kubernetes.io/component", "app.kubernetes.io/name", "app"):
        val = str(labels.get(label_key) or "")
        if val and "template_placeholder" not in val:
            return val
    stem = os.path.splitext(os.path.basename(file_name))[0]
    if stem.startswith("_") or stem in ("notes", "tests", "serviceaccount"):
        return None
    cleaned_stem = re.sub(r"[-_](deployment|service|statefulset|daemonset)$", "", stem)
    return cleaned_stem or None


def parse_helm_dir(helm_dir: str, namespace: str = "default") -> Tuple[List[TopologyNode], List[TopologyEdge]]:
    """
    Parse Kubernetes/Helm manifest files in helm_dir and extract TopologyNodes and TopologyEdges.
    
    Args:
        helm_dir: Root directory containing YAML manifest templates.
        namespace: Default namespace to assign if not explicitly declared in manifest.
        
    Returns:
        Tuple of (list of TopologyNode, list of TopologyEdge).
    """
    if not os.path.isdir(helm_dir):
        return [], []

    raw_services: Dict[str, Dict[str, Any]] = {}
    service_ports: Dict[str, Set[int]] = {}
    service_env_vars: Dict[str, List[Tuple[str, str]]] = {}
    configmap_data: Dict[str, List[Tuple[str, str]]] = {}

    for root, _, files in os.walk(helm_dir):
        for file in files:
            if not (file.endswith(".yaml") or file.endswith(".yml")):
                continue
            file_path = os.path.join(root, file)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                try:
                    docs = list(yaml.safe_load_all(content))
                except Exception:
                    # Attempt cleanup of helm template directives
                    cleaned = _clean_helm_template_text(content)
                    docs = list(yaml.safe_load_all(cleaned))

                for doc in docs:
                    if not isinstance(doc, dict):
                        continue
                    kind = doc.get("kind", "")
                    metadata = doc.get("metadata") or {}
                    labels = metadata.get("labels") or {}
                    name = _infer_manifest_name(metadata, labels, file)
                    if not name:
                        continue

                    doc_ns = metadata.get("namespace") or namespace
                    # Clean any template placeholders in namespace
                    if "template_placeholder" in str(doc_ns) or not doc_ns:
                        doc_ns = namespace

                    key = f"{name}.{doc_ns}"

                    if kind == "ConfigMap":
                        cm_entries = [
                            (str(k), str(v))
                            for k, v in (doc.get("data") or {}).items()
                            if v is not None
                        ]
                        configmap_data[name] = cm_entries
                        continue

                    if kind in ("Deployment", "StatefulSet", "DaemonSet"):
                        spec = doc.get("spec") or {}
                        replicas = spec.get("replicas", 1)
                        if not isinstance(replicas, int):
                            replicas = 1
                        
                        pod_spec = (spec.get("template") or {}).get("spec") or {}
                        containers = pod_spec.get("containers") or []

                        ports: Set[int] = set()
                        limits: Dict[str, str] = {}
                        envs: List[Tuple[str, str]] = []

                        for c in containers:
                            if not isinstance(c, dict):
                                continue
                            # Extract container ports
                            for p in c.get("ports") or []:
                                if isinstance(p, dict) and "containerPort" in p:
                                    try:
                                        ports.add(int(p["containerPort"]))
                                    except (ValueError, TypeError) as exc:
                                        logger.warning("Failed to parse containerPort %r: %s", p.get("containerPort"), exc)
                            # Extract resource limits
                            c_limits = (c.get("resources") or {}).get("limits") or {}
                            for res_k, res_v in c_limits.items():
                                limits[str(res_k)] = str(res_v)
                            # Extract env vars and ConfigMap references
                            for e in c.get("env") or []:
                                if isinstance(e, dict) and "name" in e:
                                    e_name = str(e.get("name") or "")
                                    e_val = str(e.get("value") or "")
                                    val_from = e.get("valueFrom") or {}
                                    cm_ref = val_from.get("configMapKeyRef") if isinstance(val_from, dict) else None
                                    if isinstance(cm_ref, dict) and not e_val:
                                        cm_name = str(cm_ref.get("name") or "")
                                        cm_key = str(cm_ref.get("key") or "")
                                        for k, v in configmap_data.get(cm_name, []):
                                            if k == cm_key:
                                                e_val = v
                                                break
                                    envs.append((e_name, e_val))
                            for env_from in c.get("envFrom") or []:
                                if isinstance(env_from, dict):
                                    cm_ref = env_from.get("configMapRef")
                                    if isinstance(cm_ref, dict):
                                        cm_name = str(cm_ref.get("name") or "")
                                        envs.extend(configmap_data.get(cm_name, []))

                        raw_services[key] = {
                            "id": key,
                            "name": name,
                            "namespace": doc_ns,
                            "type": kind,
                            "replicas": replicas,
                            "resource_limits": limits,
                            "ports": ports,
                            "labels": labels,
                        }
                        service_env_vars[key] = envs

                    elif kind == "Service":
                        svc_spec = doc.get("spec") or {}
                        ports = set()
                        for p in svc_spec.get("ports") or []:
                            if isinstance(p, dict):
                                port_val = p.get("port") or p.get("targetPort")
                                try:
                                    ports.add(int(port_val))
                                except (ValueError, TypeError) as exc:
                                    logger.warning("Failed to parse Service port %r: %s", port_val, exc)
                        if key not in service_ports:
                            service_ports[key] = set()
                        service_ports[key].update(ports)

            except Exception:
                continue

    # Merge service ports into deployment nodes if present
    for key, p_set in service_ports.items():
        if key in raw_services:
            raw_services[key]["ports"].update(p_set)

    # Build node map
    node_map: Dict[str, TopologyNode] = {}
    for key, data in raw_services.items():
        sorted_ports = sorted(list(data["ports"]))
        node = TopologyNode(
            id=data["id"],
            name=data["name"],
            namespace=data["namespace"],
            type=data["type"],
            replicas=data["replicas"],
            resource_limits=data["resource_limits"],
            ports=sorted_ports,
            labels=data["labels"],
            dependencies=[],
            stub=False,
        )
        node_map[key] = node

    # Second pass: dependency edge discovery from env vars
    edges: List[TopologyEdge] = []
    service_names: Dict[str, str] = {data["name"]: key for key, data in raw_services.items()}

    for key, envs in service_env_vars.items():
        source_node = node_map.get(key)
        if not source_node:
            continue

        for env_name, env_val in envs:
            # Check against all known service names
            for target_name, target_key in service_names.items():
                if target_key == key:
                    continue  # ignore self-dependency
                
                # Match target service name in env value or env name
                # Matches e.g. "cartservice:7070", "http://cartservice:7070", "cartservice.boutique"
                pattern = rf"\b{re.escape(target_name)}\b"
                if re.search(pattern, env_val, re.IGNORECASE) or re.search(pattern, env_name, re.IGNORECASE):
                    if target_key not in source_node.dependencies:
                        source_node.dependencies.append(target_key)
                    
                    # Try to extract port from env_val (e.g. "http://quote:8080/getquote" -> 8080)
                    port_match = re.search(rf"{re.escape(target_name)}(?:\.[a-z0-9-]+)*\s*:\s*(\d{{2,5}})", env_val)
                    edge_port: Optional[int] = None
                    if port_match:
                        try:
                            edge_port = int(port_match.group(1))
                        except ValueError as exc:
                            logger.warning("Failed to parse edge port from %s: %s", port_match.group(1), exc)
                    if edge_port is None and node_map[target_key].ports:
                        edge_port = node_map[target_key].ports[0]

                    # Add edge if not duplicate
                    existing_edge = any(e.from_node == source_node.id and e.to_node == target_key for e in edges)
                    if not existing_edge:
                        edges.append(
                            TopologyEdge(
                                from_node=source_node.id,
                                to_node=target_key,
                                protocol="TCP",
                                port=edge_port,
                            )
                        )

    # Sort dependencies on all nodes for determinism
    for node in node_map.values():
        node.dependencies.sort()

    nodes = sorted(list(node_map.values()), key=lambda n: n.id)
    edges.sort(key=lambda e: (e.from_node, e.to_node))

    return nodes, edges
