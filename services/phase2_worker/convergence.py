import asyncio
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from shared.config.loader import ConfigManager
from shared.config.schemas import SystemConfig, ConvergenceConfig
from shared.harness.mcp import get_mcp_client

logger = logging.getLogger(__name__)


def extract_scalar_from_prometheus_result(result_data: Any) -> Optional[float]:
    """Extract float scalar metric from Prometheus query result structure."""
    if result_data is None:
        return None

    def _to_finite_float(val: Any) -> Optional[float]:
        try:
            v = float(val)
            return v if math.isfinite(v) else None
        except (ValueError, TypeError):
            return None

    # If it's already a float or int
    if isinstance(result_data, (int, float)):
        return _to_finite_float(result_data)

    # If string
    if isinstance(result_data, str):
        return _to_finite_float(result_data)

    # If dict from MCP tool call response
    if isinstance(result_data, dict):
        # Could be FastMCP content block: {"content": [{"type": "text", "text": "..."}]}
        if "content" in result_data and isinstance(result_data["content"], list):
            for block in result_data["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_val = block.get("text", "")
                    try:
                        inner = json.loads(text_val)
                        return extract_scalar_from_prometheus_result(inner)
                    except Exception:
                        try:
                            import ast
                            inner = ast.literal_eval(text_val)
                            return extract_scalar_from_prometheus_result(inner)
                        except Exception as exc:
                            logger.warning("Failed to decode FastMCP block text: %s (text: %s)", exc, text_val[:120])

        # Could be structured result dict: {"results": [{"values": [...]}]}
        results = result_data.get("results")
        if isinstance(results, list) and results:
            first = results[0]
            values = first.get("values", [])
            if values:
                # [timestamp, "value"]
                last_pair = values[-1]
                if isinstance(last_pair, (list, tuple)) and len(last_pair) >= 2:
                    return _to_finite_float(last_pair[1])
                elif isinstance(last_pair, (int, float)):
                    return _to_finite_float(last_pair)

        # Standard Prometheus API payload: {"data": {"result": [{"value": [...]}]}}
        data_block = result_data.get("data", {})
        if isinstance(data_block, dict):
            res_list = data_block.get("result", [])
            if res_list and isinstance(res_list[0], dict):
                val_pair = res_list[0].get("value", [])
                if isinstance(val_pair, (list, tuple)) and len(val_pair) >= 2:
                    return _to_finite_float(val_pair[1])

    return None


async def execute_promql(
    query: str,
    mcp_client: Any,
) -> Optional[float]:
    """Execute PromQL query via MCP client or callable and parse scalar result."""
    try:
        if callable(mcp_client):
            res = await mcp_client(query)
            return extract_scalar_from_prometheus_result(res)

        if hasattr(mcp_client, "call_tool"):
            res = await mcp_client.call_tool("prometheus_query", {"query": query, "time_range": "2m"})
            return extract_scalar_from_prometheus_result(res)

    except Exception as e:
        logger.warning(f"Convergence Prometheus query failed for '{query}': {e}")
        return None

    return None


async def _check_convergence_checkpoint(
    redis_client: Optional[Any],
    incident_id: str,
) -> str:
    """Check if convergence polling should abort ('dropped') or hold ('halted') at a safe checkpoint."""
    if redis_client is None:
        return "ok"
    import inspect

    async def _get_val(key: str) -> Optional[str]:
        try:
            getter = getattr(redis_client, "get", None)
            if getter is None:
                return None
            if asyncio.iscoroutinefunction(getter):
                res = await getter(key)
            else:
                res = await asyncio.to_thread(getter, key)
                if inspect.isawaitable(res):
                    res = await res
            if res is None:
                return None
            if isinstance(res, (bytes, bytearray)):
                return res.decode("utf-8")
            return res if isinstance(res, str) else None
        except Exception:
            return None

    # 1. Check if incident was dropped
    raw_drop = await _get_val(f"incident:{incident_id}:dropped")
    if raw_drop:
        if raw_drop.strip() in ("1", "true", "True"):
            return "dropped"
        try:
            if json.loads(raw_drop).get("dropped"):
                return "dropped"
        except Exception:
            pass

    # 2. Check global System Halt
    raw_pause = await _get_val("system:status:paused")
    if raw_pause:
        try:
            if json.loads(raw_pause).get("paused"):
                return "halted"
        except Exception:
            pass

    # 3. Check incident-level STANDBY
    raw_state = await _get_val(f"incident:{incident_id}:state")
    if raw_state:
        try:
            if json.loads(raw_state).get("state") == "STANDBY":
                return "halted"
        except Exception:
            pass

    return "ok"


async def run_convergence_monitor(
    incident_id: str,
    affected_services: List[str],
    severity: str = "P2",
    is_stateful: bool = False,
    mcp_client: Optional[Any] = None,
    system_config: Optional[SystemConfig] = None,
    sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None,
    now_fn: Optional[Callable[[], float]] = None,
    required_clean_samples: Optional[int] = None,
    affected_namespace: Optional[str] = None,
    redis_client: Optional[Any] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Monitor recovery after remediation action:
    1. Wait for bake time to elapse (stateless vs stateful).
    2. Poll golden signals (error rate, p99 latency, CPU, memory) with mid-convergence halt/drop checkpoints.
    3. Check live pod/container readiness against Kubernetes API.
    4. Reset window on any violation.
    5. Return (True, {}) on convergence, or (False, latest_signals) on deadline timeout/drop.
    """
    sys_cfg = system_config or ConfigManager().system
    conv_cfg: ConvergenceConfig = sys_cfg.convergence
    slo = conv_cfg.slo

    _sleep = sleep_fn or asyncio.sleep
    _now = now_fn or time.time

    # Resolve or create Prometheus client
    client = mcp_client
    if client is None:
        try:
            client = get_mcp_client("prometheus")
        except Exception:
            client = None

    poll_interval = max(1, conv_cfg.poll_interval_seconds)

    async def _wait_if_halted(extend_deadline: bool = False) -> Optional[str]:
        cp_status = await _check_convergence_checkpoint(redis_client, incident_id)
        while cp_status == "halted":
            pause_step = min(poll_interval, 2.0)
            logger.info("Incident %s: Convergence holding at safe checkpoint (System Halt/Standby).", incident_id)
            await _sleep(pause_step)
            if extend_deadline:
                nonlocal deadline
                deadline += pause_step
            cp_status = await _check_convergence_checkpoint(redis_client, incident_id)
        if cp_status == "dropped":
            logger.info("Incident %s: Aborting convergence monitoring (incident dropped).", incident_id)
            return "dropped"
        return "ok"

    # Checkpoint 1a: Hold/abort before stabilization bake time
    deadline = 0.0
    if await _wait_if_halted(extend_deadline=False) == "dropped":
        return False, {"aborted": "incident_dropped"}

    # Step 1: Bake time
    bake_time = (
        conv_cfg.bake_time_seconds_stateful
        if is_stateful
        else conv_cfg.bake_time_seconds_stateless
    )
    if bake_time > 0:
        logger.info(f"Incident {incident_id}: Bake time {bake_time}s starting...")
        await _sleep(bake_time)

    # Checkpoint 1b: Hold/abort right after bake time before opening SLO window
    if await _wait_if_halted(extend_deadline=False) == "dropped":
        return False, {"aborted": "incident_dropped"}

    # Step 2: Evaluation polling loop
    window_duration = max(poll_interval, conv_cfg.window_seconds)
    target_clean_samples = required_clean_samples or max(1, window_duration // poll_interval)

    start_time = _now()
    deadline = start_time + window_duration
    consecutive_clean_samples = 0
    latest_signals: Dict[str, Any] = {}
    latest_pod_checks: List[Dict[str, Any]] = []

    target_services = affected_services or ["default"]

    def _build_result_payload() -> Dict[str, Any]:
        return {
            **latest_signals,
            "_meta": {
                "monitored_services": list(target_services),
                "workload_mode": "stateful" if is_stateful else "stateless",
                "bake_time_seconds": bake_time,
                "window_seconds": window_duration,
                "poll_interval_seconds": poll_interval,
                "clean_samples": consecutive_clean_samples,
                "target_clean_samples": target_clean_samples,
                "slo_thresholds": {
                    "error_rate_threshold": slo.error_rate_threshold,
                    "p99_latency_ms": slo.p99_latency_ms,
                    "cpu_saturation_threshold": slo.cpu_saturation_threshold,
                    "memory_saturation_threshold": slo.memory_saturation_threshold,
                },
                "pod_checks": latest_pod_checks,
            },
        }

    while True:
        # Checkpoint 2: Pause/abort before issuing PromQL read queries
        if await _wait_if_halted(extend_deadline=True) == "dropped":
            return False, {"aborted": "incident_dropped"}

        current_time = _now()
        sample_failed = False
        sample_signals: Dict[str, Any] = {}
        sample_pod_checks: List[Dict[str, Any]] = []

        queries_cfg = conv_cfg.queries

        for svc in target_services:
            # 1. Error rate: 5xx / total requests
            err_query = queries_cfg.error_rate.replace("{service}", svc)
            err_val = await execute_promql(err_query, client)
            if err_val is not None:
                sample_signals[f"{svc}.error_rate"] = err_val
                if err_val > slo.error_rate_threshold:
                    sample_failed = True
            else:
                logger.warning(
                    "Incident %s: metric error_rate for service '%s' returned None (backend down or missing metric)",
                    incident_id,
                    svc,
                )
                sample_failed = True

            # 2. P99 latency ms
            lat_query = queries_cfg.p99_latency_ms.replace("{service}", svc)
            lat_val = await execute_promql(lat_query, client)
            if lat_val is not None:
                sample_signals[f"{svc}.p99_latency_ms"] = lat_val
                if lat_val > slo.p99_latency_ms:
                    sample_failed = True
            else:
                logger.warning(
                    "Incident %s: metric p99_latency_ms for service '%s' returned None (backend down or missing metric)",
                    incident_id,
                    svc,
                )
                sample_failed = True

            # 3. CPU saturation
            cpu_query = queries_cfg.cpu_saturation.replace("{service}", svc)
            cpu_val = await execute_promql(cpu_query, client)
            if cpu_val is not None:
                sample_signals[f"{svc}.cpu_saturation"] = cpu_val
                if cpu_val > slo.cpu_saturation_threshold:
                    sample_failed = True
            else:
                logger.warning(
                    "Incident %s: metric cpu_saturation for service '%s' returned None (backend down or missing metric)",
                    incident_id,
                    svc,
                )
                sample_failed = True

            # 4. Memory saturation
            mem_query = queries_cfg.memory_saturation.replace("{service}", svc)
            mem_val = await execute_promql(mem_query, client)
            if mem_val is not None:
                sample_signals[f"{svc}.memory_saturation"] = mem_val
                if mem_val > slo.memory_saturation_threshold:
                    sample_failed = True
            else:
                logger.warning(
                    "Incident %s: metric memory_saturation for service '%s' returned None (backend down or missing metric)",
                    incident_id,
                    svc,
                )
                sample_failed = True

        # Checkpoint 3: Hold/abort before Kubernetes pod readiness check
        if await _wait_if_halted(extend_deadline=True) == "dropped":
            return False, {"aborted": "incident_dropped"}

        # 5. Kubernetes pod readiness check (ensure containers are Running and Ready, not in CrashLoop/OOM)
        if affected_namespace:
            try:
                from shared.harness.mcp import resolve_mcp_client_for_tool
                k8s_client = resolve_mcp_client_for_tool("k8s_get_pod_status")
                pod_res = await k8s_client.call_tool("k8s_get_pod_status", {"namespace": affected_namespace})
                pod_data = {}
                if isinstance(pod_res, dict):
                    if "content" in pod_res and isinstance(pod_res["content"], list):
                        for b in pod_res["content"]:
                            if isinstance(b, dict) and b.get("type") == "text":
                                try:
                                    pod_data = json.loads(b.get("text", "{}"))
                                    break
                                except Exception:
                                    pass
                    elif "pods" in pod_res:
                        pod_data = pod_res

                pods = pod_data.get("pods", [])
                for svc in target_services:
                    matching_pods = [p for p in pods if svc in p.get("name", "")]
                    if matching_pods:
                        for p in matching_pods:
                            p_name = p.get("name", "")
                            p_phase = p.get("phase", "")
                            p_ready = p.get("ready", False)
                            c_statuses = p.get("container_statuses", [])
                            has_crash = any(
                                cs.get("state", {}).get("waiting", {}).get("reason") in ("CrashLoopBackOff", "OOMKilled", "Error")
                                or cs.get("state", {}).get("terminated", {}).get("reason") in ("CrashLoopBackOff", "OOMKilled", "Error")
                                for cs in c_statuses
                            )
                            is_pod_healthy = bool(p_ready and p_phase == "Running" and not has_crash)
                            sample_pod_checks.append({
                                "pod": p_name,
                                "service": svc,
                                "phase": p_phase,
                                "ready": bool(p_ready),
                                "healthy": is_pod_healthy,
                            })
                            if not is_pod_healthy:
                                logger.warning(
                                    "Incident %s: pod '%s' not healthy (phase: %s, ready: %s, crash: %s)",
                                    incident_id,
                                    p_name,
                                    p_phase,
                                    p_ready,
                                    has_crash,
                                )
                                sample_signals[f"{p_name}.ready"] = 0
                                sample_failed = True
                            else:
                                sample_signals[f"{p_name}.ready"] = 1
            except Exception as k8s_exc:
                logger.warning("Incident %s: error checking pod readiness during convergence: %s", incident_id, k8s_exc)

        latest_signals = sample_signals
        latest_pod_checks = sample_pod_checks

        if sample_failed:
            # Reset consecutive clean samples on any SLO violation
            consecutive_clean_samples = 0
            logger.info(
                f"Incident {incident_id}: Signals outside SLO bounds. Resetting sample counter. Signals: {sample_signals}"
            )
        else:
            consecutive_clean_samples += 1
            logger.debug(
                f"Incident {incident_id}: Clean sample {consecutive_clean_samples}/{target_clean_samples}"
            )
            if consecutive_clean_samples >= target_clean_samples:
                logger.info(f"Incident {incident_id}: Convergence confirmed over full window.")
                return True, _build_result_payload()

        # Check deadline timeout
        if current_time >= deadline:
            logger.warning(
                f"Incident {incident_id}: Convergence window timed out without achieving required clean samples."
            )
            return False, _build_result_payload()

        await _sleep(poll_interval)
