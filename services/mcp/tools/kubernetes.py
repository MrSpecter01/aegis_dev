import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx
import yaml
from mcp.server.fastmcp import FastMCP

from shared.config.loader import ConfigManager

logger = logging.getLogger("aegis.mcp.tools.kubernetes")

K8S_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
LABEL_SELECTOR_PATTERN = re.compile(r"^[a-zA-Z0-9_./=!,() -]*$")


def validate_k8s_name(name: str, field_name: str = "name") -> None:
    if not name or not name.strip():
        raise ValueError(f"{field_name} cannot be empty")
    cleaned = name.strip()
    if len(cleaned) > 253:
        raise ValueError(f"{field_name} exceeds maximum length of 253 characters")
    if not K8S_NAME_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid {field_name}: '{name}'. Must be a valid Kubernetes DNS label/subdomain."
        )


def validate_label_selector(selector: str) -> None:
    if selector and len(selector) > 253:
        raise ValueError("label_selector exceeds maximum length of 253 characters")
    if selector and not LABEL_SELECTOR_PATTERN.match(selector):
        raise ValueError(f"Invalid characters in label_selector: '{selector}'")


def resolve_k8s_config() -> Tuple[str, Dict[str, str], bool]:
    """
    Resolve Kubernetes API endpoint, headers, and verify_ssl flag.
    Supports system config mcp_endpoints, KUBECONFIG file, or in-cluster token.
    """
    config = ConfigManager()
    mcp_endpoints = config.system.mcp_endpoints

    if mcp_endpoints.kubernetes_url:
        headers = {}
        if mcp_endpoints.kubernetes_token:
            headers["Authorization"] = f"Bearer {mcp_endpoints.kubernetes_token}"
        return mcp_endpoints.kubernetes_url.rstrip("/"), headers, False

    # Check kubeconfig file path
    kubeconfig_path = mcp_endpoints.kubeconfig_path or os.getenv("KUBECONFIG")
    if not kubeconfig_path:
        # Check standard user home directory
        home = Path.home()
        default_path = home / ".kube" / "config"
        if default_path.exists():
            kubeconfig_path = str(default_path)

    if kubeconfig_path and Path(kubeconfig_path).exists():
        try:
            with open(kubeconfig_path, "r", encoding="utf-8") as f:
                kdata = yaml.safe_load(f) or {}

            current_ctx = kdata.get("current-context")
            contexts = {c["name"]: c.get("context", {}) for c in kdata.get("contexts", [])}
            clusters = {c["name"]: c.get("cluster", {}) for c in kdata.get("clusters", [])}
            users = {u["name"]: u.get("user", {}) for u in kdata.get("users", [])}

            ctx = contexts.get(current_ctx, {})
            cluster_name = ctx.get("cluster")
            user_name = ctx.get("user")

            cluster = clusters.get(cluster_name, {})
            server = cluster.get("server", "http://localhost:8001")

            headers = {}
            user = users.get(user_name, {})
            if "token" in user:
                headers["Authorization"] = f"Bearer {user['token']}"

            return server.rstrip("/"), headers, False
        except Exception as exc:
            logger.warning("Could not parse kubeconfig at %s: %s", kubeconfig_path, exc)

    # Check in-cluster service account
    in_cluster_token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    if in_cluster_token_path.exists():
        token = in_cluster_token_path.read_text(encoding="utf-8").strip()
        return "https://kubernetes.default.svc", {"Authorization": f"Bearer {token}"}, False

    # Default fallback
    return "http://localhost:8001", {}, False


async def k8s_get_events(
    namespace: str,
    resource: str = "",
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Retrieve Kubernetes events for a namespace and optional resource."""
    validate_k8s_name(namespace, "namespace")
    if resource:
        validate_k8s_name(resource, "resource")

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/api/v1/namespaces/{namespace}/events"

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, headers=headers or {}, timeout=10.0)
            if resp.status_code == 404:
                return {
                    "namespace": namespace,
                    "resource": resource,
                    "event_count": 0,
                    "events": [],
                    "message": f"Namespace '{namespace}' not found",
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Kubernetes HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Kubernetes connection error: {str(e)}") from e

        items = data.get("items", [])
        events: List[Dict[str, Any]] = []
        for item in items:
            inv_obj = item.get("involvedObject", {})
            inv_name = inv_obj.get("name", "")
            if resource and inv_name != resource:
                continue

            events.append({
                "reason": item.get("reason"),
                "message": item.get("message"),
                "type": item.get("type"),
                "count": item.get("count", 1),
                "first_timestamp": item.get("firstTimestamp"),
                "last_timestamp": item.get("lastTimestamp"),
                "involved_object": inv_name,
                "kind": inv_obj.get("kind"),
            })

        return {
            "namespace": namespace,
            "resource": resource,
            "event_count": len(events),
            "events": events,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as new_client:
        return await _execute(new_client)


async def k8s_get_pod_status(
    namespace: str,
    label_selector: str = "",
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Retrieve pod status and container health in a namespace."""
    validate_k8s_name(namespace, "namespace")
    validate_label_selector(label_selector)

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/api/v1/namespaces/{namespace}/pods"
    params = {}
    if label_selector:
        params["labelSelector"] = label_selector

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, params=params, headers=headers or {}, timeout=10.0)
            if resp.status_code == 404:
                return {
                    "namespace": namespace,
                    "label_selector": label_selector,
                    "pod_count": 0,
                    "pods": [],
                    "message": f"Namespace '{namespace}' not found",
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Kubernetes HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Kubernetes connection error: {str(e)}") from e

        items = data.get("items", [])
        pods: List[Dict[str, Any]] = []
        for item in items:
            metadata = item.get("metadata", {})
            status = item.get("status", {})
            container_statuses = status.get("containerStatuses", [])
            total_restarts = sum(cs.get("restartCount", 0) for cs in container_statuses)
            all_ready = bool(container_statuses and all(cs.get("ready", False) for cs in container_statuses))

            pods.append({
                "name": metadata.get("name"),
                "phase": status.get("phase"),
                "ready": all_ready,
                "restart_count": total_restarts,
                "node_name": item.get("spec", {}).get("nodeName"),
                "container_statuses": [
                    {
                        "name": cs.get("name"),
                        "ready": cs.get("ready"),
                        "restart_count": cs.get("restartCount"),
                        "state": cs.get("state", {}),
                    }
                    for cs in container_statuses
                ],
            })

        return {
            "namespace": namespace,
            "label_selector": label_selector,
            "pod_count": len(pods),
            "pods": pods,
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as new_client:
        return await _execute(new_client)


async def k8s_get_node_metrics(
    node: str,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Retrieve node metrics (CPU and Memory usage) via metrics.k8s.io."""
    validate_k8s_name(node, "node")

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/apis/metrics.k8s.io/v1beta1/nodes/{node}"

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, headers=headers or {}, timeout=10.0)
            if resp.status_code == 404:
                return {
                    "node": node,
                    "available": False,
                    "usage": {},
                    "message": f"Metrics not found for node '{node}' (metrics-server may not be installed)",
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Kubernetes HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Kubernetes connection error: {str(e)}") from e

        usage = data.get("usage", {})
        return {
            "node": node,
            "available": True,
            "usage": {
                "cpu": usage.get("cpu"),
                "memory": usage.get("memory"),
            },
            "timestamp": data.get("timestamp"),
            "window": data.get("window"),
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as new_client:
        return await _execute(new_client)


async def k8s_get_configmap_diff(
    namespace: str,
    name: str,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Retrieve ConfigMap data and change annotations."""
    validate_k8s_name(namespace, "namespace")
    validate_k8s_name(name, "name")

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/api/v1/namespaces/{namespace}/configmaps/{name}"

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.get(endpoint, headers=headers or {}, timeout=10.0)
            if resp.status_code == 404:
                return {
                    "namespace": namespace,
                    "name": name,
                    "found": False,
                    "data": {},
                    "message": f"ConfigMap '{name}' in namespace '{namespace}' not found",
                }
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Kubernetes HTTP error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            raise RuntimeError(f"Kubernetes connection error: {str(e)}") from e

        metadata = data.get("metadata", {})
        annotations = metadata.get("annotations", {})
        cm_data = data.get("data", {})

        return {
            "namespace": namespace,
            "name": name,
            "found": True,
            "data_keys": list(cm_data.keys()),
            "data": cm_data,
            "last_applied_configuration": annotations.get("kubectl.kubernetes.io/last-applied-configuration"),
            "resource_version": metadata.get("resourceVersion"),
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as new_client:
        return await _execute(new_client)


async def k8s_get_pod_diagnostics(
    namespace: str,
    service_or_pod: str,
    client: Optional[httpx.AsyncClient] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Retrieve composite pod health, termination details, OOM reasons, and recent warning events in one call."""
    validate_k8s_name(namespace, "namespace")
    validate_k8s_name(service_or_pod, "service_or_pod")

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        # 1. Fetch pods in namespace
        pod_endpoint = f"{base_url.rstrip('/')}/api/v1/namespaces/{namespace}/pods"
        try:
            pod_resp = await c.get(pod_endpoint, headers=headers or {}, timeout=10.0)
            if pod_resp.status_code == 404:
                return {
                    "namespace": namespace,
                    "service_or_pod": service_or_pod,
                    "found": False,
                    "error": f"Namespace '{namespace}' not found.",
                }
            pod_resp.raise_for_status()
            pod_data = pod_resp.json()
        except Exception as e:
            return {
                "namespace": namespace,
                "service_or_pod": service_or_pod,
                "found": False,
                "error": f"Kubernetes API error fetching pods: {str(e)}",
            }

        # Match pods by label or name
        matched_pods = []
        for item in pod_data.get("items", []):
            p_name = item.get("metadata", {}).get("name", "")
            p_labels = item.get("metadata", {}).get("labels", {}) or {}
            if (
                p_name == service_or_pod
                or p_name.startswith(f"{service_or_pod}-")
                or p_labels.get("app") == service_or_pod
                or p_labels.get("name") == service_or_pod
                or p_labels.get("app.kubernetes.io/name") == service_or_pod
            ):
                matched_pods.append(item)

        # 2. Fetch warning events in namespace
        event_endpoint = f"{base_url.rstrip('/')}/api/v1/namespaces/{namespace}/events"
        matched_events = []
        try:
            ev_resp = await c.get(event_endpoint, headers=headers or {}, timeout=10.0)
            if ev_resp.status_code == 200:
                ev_items = ev_resp.json().get("items", [])
                matched_pod_names = {p.get("metadata", {}).get("name", "") for p in matched_pods}
                for ev in ev_items:
                    ev_type = ev.get("type", "Normal")
                    inv_obj = ev.get("involvedObject", {})
                    inv_name = inv_obj.get("name", "")
                    if inv_name in matched_pod_names or inv_name == service_or_pod or inv_name.startswith(f"{service_or_pod}-"):
                        matched_events.append({
                            "type": ev_type,
                            "reason": ev.get("reason"),
                            "message": ev.get("message"),
                            "count": ev.get("count", 1),
                            "last_timestamp": ev.get("lastTimestamp"),
                            "object": f"{inv_obj.get('kind')}/{inv_name}",
                        })
        except Exception as ev_err:
            logger.warning("Failed to fetch events during pod diagnostics: %s", ev_err)

        # 3. Summarize container states, exit codes, OOM reasons
        pod_summaries = []
        critical_issues = []
        for p in matched_pods:
            m = p.get("metadata", {})
            s = p.get("status", {})
            c_statuses = s.get("containerStatuses", [])
            containers = []
            for cs in c_statuses:
                state = cs.get("state", {})
                last_state = cs.get("lastState", {})
                term_info = state.get("terminated") or last_state.get("terminated")
                waiting_info = state.get("waiting")

                c_info = {
                    "container": cs.get("name"),
                    "ready": cs.get("ready"),
                    "restart_count": cs.get("restartCount", 0),
                    "state": list(state.keys())[0] if state else "unknown",
                }
                if term_info:
                    c_info["terminated_reason"] = term_info.get("reason")
                    c_info["exit_code"] = term_info.get("exitCode")
                    if term_info.get("reason") == "OOMKilled":
                        critical_issues.append(f"Container '{cs.get('name')}' terminated with OOMKilled (Exit 137).")
                    elif term_info.get("exitCode") == 137:
                        critical_issues.append(f"Container '{cs.get('name')}' terminated by external signal SIGKILL (Exit 137, Reason: {term_info.get('reason', 'Error')}).")
                if waiting_info:
                    c_info["waiting_reason"] = waiting_info.get("reason")
                    c_info["waiting_message"] = waiting_info.get("message")
                    if waiting_info.get("reason") in ("CrashLoopBackOff", "ImagePullBackOff"):
                        critical_issues.append(f"Container '{cs.get('name')}' waiting with {waiting_info.get('reason')}.")
                containers.append(c_info)

            pod_summaries.append({
                "name": m.get("name"),
                "phase": s.get("phase"),
                "node": p.get("spec", {}).get("nodeName"),
                "containers": containers,
            })

        return {
            "namespace": namespace,
            "service_or_pod": service_or_pod,
            "found": len(matched_pods) > 0,
            "pod_count": len(matched_pods),
            "pods": pod_summaries,
            "critical_issues": critical_issues,
            "recent_events": matched_events[-10:] if matched_events else [],
        }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as new_client:
        return await _execute(new_client)


def register_tools(mcp: FastMCP) -> None:
    """Register Kubernetes read tools onto a FastMCP instance."""
    @mcp.tool(name="k8s_get_events")
    async def mcp_k8s_get_events(namespace: str, resource: str = "") -> Dict[str, Any]:
        """Get Kubernetes events in a namespace with optional resource filtering."""
        return await k8s_get_events(namespace, resource)

    @mcp.tool(name="k8s_get_pod_status")
    async def mcp_k8s_get_pod_status(namespace: str, label_selector: str = "") -> Dict[str, Any]:
        """Get pod statuses and container restart counts in a namespace."""
        return await k8s_get_pod_status(namespace, label_selector)

    @mcp.tool(name="k8s_get_pod_diagnostics")
    async def mcp_k8s_get_pod_diagnostics(namespace: str, service_or_pod: str) -> Dict[str, Any]:
        """Get composite pod health, termination details, OOM reasons, and recent events in a single call."""
        return await k8s_get_pod_diagnostics(namespace, service_or_pod)

    @mcp.tool(name="k8s_get_node_metrics")
    async def mcp_k8s_get_node_metrics(node: str) -> Dict[str, Any]:
        """Get node metrics (CPU and Memory) via metrics.k8s.io."""
        return await k8s_get_node_metrics(node)

    @mcp.tool(name="k8s_get_configmap_diff")
    async def mcp_k8s_get_configmap_diff(namespace: str, name: str) -> Dict[str, Any]:
        """Get ConfigMap details and change annotations in a namespace."""
        return await k8s_get_configmap_diff(namespace, name)
