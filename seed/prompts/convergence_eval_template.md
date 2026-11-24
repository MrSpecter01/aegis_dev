# Convergence Evaluation Template

## Target Service & Scope
- Service: {service_name}
- Namespace: {namespace}
- Severity: {severity}
- Action Executed: {action_name} (Action ID: {action_id})
- Elapsed Bake Time: {elapsed_bake_seconds}s (Required: {required_bake_seconds}s)

## Golden Signal Telemetry
1. HTTP 5xx Error Rate:
   - Value: {error_rate_value}
   - SLO Limit: {error_rate_threshold}
   - Status: {error_rate_status}

2. p99 Request Latency:
   - Value: {p99_latency_ms}ms
   - SLO Limit: {p99_latency_threshold_ms}ms
   - Status: {p99_latency_status}

3. CPU Saturation:
   - Value: {cpu_saturation_ratio}
   - SLO Limit: {cpu_saturation_threshold}
   - Status: {cpu_saturation_status}

4. Memory Saturation:
   - Value: {memory_saturation_ratio}
   - SLO Limit: {memory_saturation_threshold}
   - Status: {memory_saturation_status}

## Convergence Conclusion
- Window Stability: {window_stability_ratio}% compliant over {evaluation_window_seconds}s
- Overall Decision: {convergence_verdict} (CONVERGED | NOT_CONVERGED | ESCALATE)
