import json
from typing import Any, Dict, List, Optional
import httpx
from mcp.server.fastmcp import FastMCP

from shared.sag.validator import (
    check_injection,
    get_redis_client,
    validate_grace_period,
    validate_incident_scope,
    validate_k8s_name,
    validate_replicas,
    validate_resource_limits,
    verify_action_approved,
)
from services.mcp.tools.kubernetes import resolve_k8s_config
from shared.config.loader import ConfigManager


async def restart_pod(
    namespace: str,
    pod_name: str,
    incident_id: str,
    action_id: str,
    client: Optional[httpx.AsyncClient] = None,
    redis_client: Optional[Any] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Restart (delete) a Kubernetes pod to recover from crashed or OOM states."""
    try:
        check_injection({
            "namespace": namespace,
            "pod_name": pod_name,
            "incident_id": incident_id,
            "action_id": action_id,
        })
        validate_k8s_name(namespace, "namespace")
        validate_k8s_name(pod_name, "pod_name")
        r_client = redis_client if redis_client is not None else await get_redis_client()
        await verify_action_approved(r_client, incident_id, action_id, tool_name="restart_pod")
        await validate_incident_scope(r_client, incident_id, namespace=namespace, target_resource=pod_name, tool_name="restart_pod")
    except (ValueError, PermissionError) as exc:
        return {"success": False, "error": str(exc), "isError": True}

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/api/v1/namespaces/{namespace}/pods/{pod_name}"

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.delete(endpoint, headers=headers or {}, timeout=15.0)
            if resp.status_code == 404:
                # Fallback: check if pod_name was passed as a service or deployment name
                list_resp = await c.get(
                    f"{base_url.rstrip('/')}/api/v1/namespaces/{namespace}/pods",
                    headers=headers or {},
                    timeout=10.0,
                )
                if list_resp.status_code == 200:
                    items = list_resp.json().get("items", [])
                    matched = [
                        p["metadata"]["name"]
                        for p in items
                        if p.get("metadata", {}).get("name", "").startswith(f"{pod_name}-")
                        or p.get("metadata", {}).get("labels", {}).get("name") == pod_name
                        or p.get("metadata", {}).get("labels", {}).get("app") == pod_name
                    ]
                    if matched:
                        real_pod = matched[0]
                        fallback_resp = await c.delete(
                            f"{base_url.rstrip('/')}/api/v1/namespaces/{namespace}/pods/{real_pod}",
                            headers=headers or {},
                            timeout=15.0,
                        )
                        if fallback_resp.status_code in (200, 202):
                            return {
                                "success": True,
                                "namespace": namespace,
                                "pod_name": real_pod,
                                "action_id": action_id,
                                "message": f"Resolved '{pod_name}' to pod '{real_pod}' and restarted successfully.",
                            }
                return {
                    "success": False,
                    "isError": True,
                    "error": f"Pod '{pod_name}' in namespace '{namespace}' not found.",
                }
            resp.raise_for_status()
            return {
                "success": True,
                "namespace": namespace,
                "pod_name": pod_name,
                "action_id": action_id,
                "message": f"Pod '{pod_name}' in namespace '{namespace}' deleted successfully (restarting).",
            }
        except httpx.HTTPStatusError as e:
            return {
                "success": False,
                "isError": True,
                "error": f"Kubernetes API error {e.response.status_code}: {e.response.text}",
            }
        except httpx.RequestError as e:
            return {
                "success": False,
                "isError": True,
                "error": f"Kubernetes connection error: {str(e)}",
            }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as c:
        return await _execute(c)


async def scale_deployment(
    namespace: str,
    deployment: str,
    replicas: int,
    incident_id: str,
    action_id: str,
    client: Optional[httpx.AsyncClient] = None,
    redis_client: Optional[Any] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Scale a Kubernetes deployment to the specified replica count (1-20)."""
    try:
        check_injection({
            "namespace": namespace,
            "deployment": deployment,
            "incident_id": incident_id,
            "action_id": action_id,
        })
        validate_k8s_name(namespace, "namespace")
        validate_k8s_name(deployment, "deployment")
        r_client = redis_client if redis_client is not None else await get_redis_client()
        await verify_action_approved(r_client, incident_id, action_id, tool_name="scale_deployment")
        await validate_incident_scope(r_client, incident_id, namespace=namespace, target_resource=deployment, tool_name="scale_deployment")
        try:
            cfg = ConfigManager().sag
            tool_policy = cfg.get_tool_policy("scale_deployment")
            max_reps = tool_policy.max_replicas or 20
        except Exception:
            max_reps = 20

        validate_replicas(replicas, max_replicas=max_reps)
    except (ValueError, PermissionError) as exc:
        return {"success": False, "error": str(exc), "isError": True}

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}/scale"
    req_headers = dict(headers or {})
    req_headers["Content-Type"] = "application/merge-patch+json"
    payload = {"spec": {"replicas": replicas}}

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            resp = await c.patch(endpoint, json=payload, headers=req_headers, timeout=15.0)
            if resp.status_code == 404:
                return {
                    "success": False,
                    "isError": True,
                    "error": f"Deployment '{deployment}' in namespace '{namespace}' not found.",
                }
            resp.raise_for_status()
            return {
                "success": True,
                "namespace": namespace,
                "deployment": deployment,
                "replicas": replicas,
                "action_id": action_id,
                "message": f"Deployment '{deployment}' scaled to {replicas} replicas.",
            }
        except httpx.HTTPStatusError as e:
            return {
                "success": False,
                "isError": True,
                "error": f"Kubernetes API error {e.response.status_code}: {e.response.text}",
            }
        except httpx.RequestError as e:
            return {
                "success": False,
                "isError": True,
                "error": f"Kubernetes connection error: {str(e)}",
            }

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as c:
        return await _execute(c)


async def rollback_release(
    namespace: str,
    app: str,
    revision: int,
    incident_id: str,
    action_id: str,
    client: Optional[httpx.AsyncClient] = None,
    redis_client: Optional[Any] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Roll back an application release to a target revision."""
    try:
        check_injection({
            "namespace": namespace,
            "app": app,
            "incident_id": incident_id,
            "action_id": action_id,
        })
        validate_k8s_name(namespace, "namespace")
        validate_k8s_name(app, "app")
        r_client = redis_client if redis_client is not None else await get_redis_client()
        await verify_action_approved(r_client, incident_id, action_id, tool_name="rollback_deployment")
        await validate_incident_scope(r_client, incident_id, namespace=namespace, target_resource=app, tool_name="rollback_release")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError(f"revision must be an integer >= 0, got {revision}")
    except (ValueError, PermissionError) as exc:
        return {"success": False, "error": str(exc), "isError": True}

    config = ConfigManager()
    argocd_url = config.system.mcp_endpoints.argocd_url
    argocd_token = config.system.mcp_endpoints.argocd_token

    if argocd_url:
        endpoint = f"{argocd_url.rstrip('/')}/api/v1/applications/{app}/rollback"
        req_headers = {"Authorization": f"Bearer {argocd_token}"} if argocd_token else {}

        async def _execute_argocd(c: httpx.AsyncClient) -> Dict[str, Any]:
            try:
                resp = await c.post(endpoint, json={"revision": str(revision)}, headers=req_headers, timeout=20.0)
                resp.raise_for_status()
                return {
                    "success": True,
                    "target": "argocd",
                    "app": app,
                    "revision": revision,
                    "action_id": action_id,
                    "message": f"ArgoCD release '{app}' rolled back to revision {revision}.",
                }
            except Exception as e:
                return {"success": False, "isError": True, "error": f"ArgoCD rollback error: {str(e)}"}

        if client:
            return await _execute_argocd(client)
        async with httpx.AsyncClient(verify=False) as c:
            return await _execute_argocd(c)

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/apis/apps/v1/namespaces/{namespace}/deployments/{app}"
    req_headers = dict(headers or {})
    req_headers["Content-Type"] = "application/strategic-merge-patch+json"

    async def _execute_k8s(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            # Query existing ReplicaSets for this app to locate target revision template
            rs_endpoint = f"{base_url.rstrip('/')}/apis/apps/v1/namespaces/{namespace}/replicasets?labelSelector=app={app}"
            target_template = None
            resolved_rev = revision
            try:
                rs_resp = await c.get(rs_endpoint, headers=req_headers, timeout=10.0)
                if rs_resp.status_code == 200:
                    rs_items = rs_resp.json().get("items", [])
                    if rs_items:
                        def _get_rev(rs_item):
                            raw_rev = rs_item.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision", "0")
                            try:
                                return int(raw_rev)
                            except (ValueError, TypeError):
                                return 0
                        sorted_rs = sorted(rs_items, key=_get_rev, reverse=True)
                        if revision > 0:
                            matching = [rs for rs in sorted_rs if _get_rev(rs) == revision]
                            if matching:
                                target_template = matching[0].get("spec", {}).get("template", {})
                                resolved_rev = revision
                        else:
                            # revision 0 means rollback to previous revision
                            if len(sorted_rs) > 1:
                                target_template = sorted_rs[1].get("spec", {}).get("template", {})
                                resolved_rev = _get_rev(sorted_rs[1])
            except Exception as rs_exc:
                pass

            if target_template and "spec" in target_template:
                patch_payload = {
                    "spec": {
                        "template": target_template
                    }
                }
            else:
                patch_payload = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "aegis.sre/rollback-revision": str(resolved_rev),
                                }
                            }
                        }
                    }
                }

            resp = await c.patch(endpoint, json=patch_payload, headers=req_headers, timeout=15.0)
            if resp.status_code == 404:
                return {
                    "success": False,
                    "isError": True,
                    "error": f"Deployment '{app}' in namespace '{namespace}' not found.",
                }
            resp.raise_for_status()
            return {
                "success": True,
                "target": "kubernetes",
                "namespace": namespace,
                "app": app,
                "revision": resolved_rev,
                "action_id": action_id,
                "message": f"Deployment '{app}' rolled back to revision {resolved_rev} via Kubernetes API.",
            }
        except Exception as e:
            return {"success": False, "isError": True, "error": f"Kubernetes rollback error: {str(e)}"}

    if client:
        return await _execute_k8s(client)
    async with httpx.AsyncClient(verify=False) as c:
        return await _execute_k8s(c)


async def update_resource_limits(
    namespace: str,
    deployment: str,
    limits: Dict[str, Any],
    incident_id: str,
    action_id: str,
    container_name: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    redis_client: Optional[Any] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Update container cpu and/or memory resource limits for a deployment."""
    actual_container = container_name or deployment
    try:
        check_injection({
            "namespace": namespace,
            "deployment": deployment,
            "container_name": actual_container,
            "incident_id": incident_id,
            "action_id": action_id,
        })
        validate_k8s_name(namespace, "namespace")
        validate_k8s_name(deployment, "deployment")
        r_client = redis_client if redis_client is not None else await get_redis_client()
        await verify_action_approved(r_client, incident_id, action_id, tool_name="patch_deployment_resources")
        await validate_incident_scope(r_client, incident_id, namespace=namespace, target_resource=deployment, tool_name="patch_deployment_resources")
        validate_resource_limits(limits)
    except (ValueError, PermissionError) as exc:
        return {"success": False, "error": str(exc), "isError": True}

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    endpoint = f"{base_url.rstrip('/')}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
    req_headers = dict(headers or {})
    req_headers["Content-Type"] = "application/strategic-merge-patch+json"
    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": actual_container,
                            "resources": {
                                "limits": limits
                            }
                        }
                    ]
                }
            }
        }
    }

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            target_container = container_name
            if not target_container:
                try:
                    dep_get = await c.get(endpoint, headers=req_headers, timeout=10.0)
                    if dep_get.status_code == 200:
                        dep_data = dep_get.json()
                        c_list = dep_data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                        names = [cont.get("name") for cont in c_list if cont.get("name")]
                        if deployment in names:
                            target_container = deployment
                        elif names:
                            target_container = names[0]
                except Exception:
                    pass
            if not target_container:
                target_container = deployment

            patch_payload = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": target_container,
                                    "resources": {
                                        "limits": limits
                                    }
                                }
                            ]
                        }
                    }
                }
            }

            resp = await c.patch(endpoint, json=patch_payload, headers=req_headers, timeout=15.0)
            if resp.status_code == 404:
                return {
                    "success": False,
                    "isError": True,
                    "error": f"Deployment '{deployment}' in namespace '{namespace}' not found.",
                }
            resp.raise_for_status()
            return {
                "success": True,
                "namespace": namespace,
                "deployment": deployment,
                "container": target_container,
                "limits": limits,
                "action_id": action_id,
                "message": f"Resource limits updated for deployment '{deployment}' (container '{target_container}').",
            }
        except Exception as e:
            return {"success": False, "isError": True, "error": f"Resource limit update error: {str(e)}"}

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as c:
        return await _execute(c)


async def drain_node(
    node: str,
    grace_period_seconds: int,
    incident_id: str,
    action_id: str,
    client: Optional[httpx.AsyncClient] = None,
    redis_client: Optional[Any] = None,
    base_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Cordon a node and evict its non-daemon pods safely."""
    try:
        check_injection({
            "node": node,
            "incident_id": incident_id,
            "action_id": action_id,
        })
        validate_k8s_name(node, "node")
        r_client = redis_client if redis_client is not None else await get_redis_client()
        await verify_action_approved(r_client, incident_id, action_id, tool_name="drain_node")
        await validate_incident_scope(r_client, incident_id, namespace=None, target_resource=node, tool_name="drain_node")
        validate_grace_period(grace_period_seconds, min_seconds=60, max_seconds=600)
    except (ValueError, PermissionError) as exc:
        return {"success": False, "error": str(exc), "isError": True}

    if not base_url:
        resolved_url, resolved_headers, _ = resolve_k8s_config()
        base_url = resolved_url
        if headers is None:
            headers = resolved_headers

    cordon_endpoint = f"{base_url.rstrip('/')}/api/v1/nodes/{node}"
    req_headers = dict(headers or {})
    req_headers["Content-Type"] = "application/strategic-merge-patch+json"
    cordon_payload = {"spec": {"unschedulable": True}}

    async def _execute(c: httpx.AsyncClient) -> Dict[str, Any]:
        try:
            # 1. Cordon the node
            c_resp = await c.patch(cordon_endpoint, json=cordon_payload, headers=req_headers, timeout=15.0)
            if c_resp.status_code == 404:
                return {"success": False, "isError": True, "error": f"Node '{node}' not found."}
            c_resp.raise_for_status()

            # 2. List pods scheduled on this node
            pods_endpoint = f"{base_url.rstrip('/')}/api/v1/pods?fieldSelector=spec.nodeName={node}"
            p_resp = await c.get(pods_endpoint, headers=headers or {}, timeout=15.0)
            evicted_count = 0
            if p_resp.status_code == 200:
                pod_items = p_resp.json().get("items", [])
                for p in pod_items:
                    p_name = p.get("metadata", {}).get("name")
                    p_ns = p.get("metadata", {}).get("namespace", "default")
                    if p_name:
                        evict_endpoint = f"{base_url.rstrip('/')}/api/v1/namespaces/{p_ns}/pods/{p_name}/eviction"
                        evict_payload = {
                            "apiVersion": "policy/v1",
                            "kind": "Eviction",
                            "metadata": {"name": p_name, "namespace": p_ns},
                            "deleteOptions": {"gracePeriodSeconds": grace_period_seconds},
                        }
                        await c.post(evict_endpoint, json=evict_payload, headers=headers or {}, timeout=10.0)
                        evicted_count += 1

            return {
                "success": True,
                "node": node,
                "cordoned": True,
                "evicted_count": evicted_count,
                "grace_period_seconds": grace_period_seconds,
                "action_id": action_id,
                "message": f"Node '{node}' cordoned successfully; {evicted_count} pods targeted for eviction.",
            }
        except Exception as e:
            return {"success": False, "isError": True, "error": f"Node drain error: {str(e)}"}

    if client:
        return await _execute(client)
    async with httpx.AsyncClient(verify=False) as c:
        return await _execute(c)


def register_write_tools(mcp: FastMCP) -> None:
    """Register all Kubernetes write tools with FastMCP server."""

    @mcp.tool(name="kubernetes_restart_pod", description="Restart (delete) a Kubernetes pod to clear crashloops or OOM states.")
    @mcp.tool(name="restart_pod", description="Restart (delete) a Kubernetes pod to clear crashloops or OOM states.")
    async def _restart_pod(namespace: str, pod_name: str, incident_id: str, action_id: str) -> Dict[str, Any]:
        return await restart_pod(namespace=namespace, pod_name=pod_name, incident_id=incident_id, action_id=action_id)

    @mcp.tool(name="kubernetes_scale_deployment", description="Scale a Kubernetes deployment to the specified replica count (1-20).")
    @mcp.tool(name="scale_deployment", description="Scale a Kubernetes deployment to the specified replica count (1-20).")
    async def _scale_deployment(namespace: str, deployment: str, replicas: int, incident_id: str, action_id: str) -> Dict[str, Any]:
        return await scale_deployment(namespace=namespace, deployment=deployment, replicas=replicas, incident_id=incident_id, action_id=action_id)

    @mcp.tool(name="kubernetes_rollback_release", description="Roll back an application release or deployment to a target revision.")
    @mcp.tool(name="rollback_release", description="Roll back an application release or deployment to a target revision.")
    async def _rollback_release(namespace: str, app: str, revision: int, incident_id: str, action_id: str) -> Dict[str, Any]:
        return await rollback_release(namespace=namespace, app=app, revision=revision, incident_id=incident_id, action_id=action_id)

    @mcp.tool(name="kubernetes_update_resource_limits", description="Update cpu and/or memory resource limits for a deployment container.")
    @mcp.tool(name="update_resource_limits", description="Update cpu and/or memory resource limits for a deployment container.")
    async def _update_resource_limits(namespace: str, deployment: str, limits: Dict[str, Any], incident_id: str, action_id: str, container_name: Optional[str] = None) -> Dict[str, Any]:
        return await update_resource_limits(namespace=namespace, deployment=deployment, container_name=container_name, limits=limits, incident_id=incident_id, action_id=action_id)

    @mcp.tool(name="kubernetes_drain_node", description="Cordon a node and evict pods gracefully (grace_period: 60-600s).")
    @mcp.tool(name="drain_node", description="Cordon a node and evict pods gracefully (grace_period: 60-600s).")
    async def _drain_node(node: str, grace_period_seconds: int, incident_id: str, action_id: str) -> Dict[str, Any]:
        return await drain_node(node=node, grace_period_seconds=grace_period_seconds, incident_id=incident_id, action_id=action_id)
