import argparse
import asyncio
import json
import sys
from typing import Any, Optional

from shared.config.loader import ConfigManager
from shared.sag.validator import is_tool_allowed


async def check_approval_status(
    redis_client: Any,
    incident_id: str,
    action_id: str,
    tool_name: Optional[str] = None,
) -> int:
    """
    Check action approval status in Redis.
    
    Returns:
      0: Approved (proceed with execution)
      1: Denied / Permanently Blocked / Tool not in proposal (abort)
      2: Pending / Not Found (pause, trigger approval flow)
    """
    if redis_client is None:
        return 2

    key = f"incident:{incident_id}:action:{action_id}"
    get_fn = getattr(redis_client, "get", None)
    if asyncio.iscoroutinefunction(get_fn):
        raw = await redis_client.get(key)
    elif callable(get_fn):
        raw = redis_client.get(key)
    else:
        raw = None

    if not raw:
        return 2

    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")

    if isinstance(raw, str):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return 1
    elif isinstance(raw, dict):
        record = raw
    else:
        return 1

    status = record.get("status")
    if status == "approved":
        if tool_name is not None:
            actions_list = record.get("proposal", {}).get("actions", [])
            if not actions_list and "actions" in record:
                actions_list = record.get("actions", [])
            allowed = {a.get("tool_name") for a in actions_list if isinstance(a, dict)}
            if record.get("tool_name"):
                allowed.add(record.get("tool_name"))
            if allowed and not is_tool_allowed(tool_name, allowed):
                return 1
        return 0
    elif status == "denied":
        return 1
    elif status in ("steered", "timed_out", "expired"):
        return 1
    elif status == "pending":
        return 2
    else:
        return 1


async def _main_cli(args: argparse.Namespace) -> int:
    try:
        import redis.asyncio as aioredis
    except ImportError:
        sys.stderr.write("redis library not installed\n")
        return 1

    redis_url = args.redis_url
    if not redis_url:
        config = ConfigManager()
        redis_url = config.system.redis.url

    client = aioredis.from_url(redis_url, decode_responses=True)
    try:
        exit_code = await check_approval_status(client, args.incident_id, args.action_id)
        if exit_code == 0:
            sys.stdout.write(f"APPROVED: Action '{args.action_id}' is approved for incident '{args.incident_id}'.\n")
        elif exit_code == 1:
            sys.stderr.write(f"DENIED: Action '{args.action_id}' was DENIED for incident '{args.incident_id}'.\n")
        else:
            sys.stdout.write(f"PENDING: Action '{args.action_id}' requires approval for incident '{args.incident_id}'.\n")
        return exit_code
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Safety Action Gateway - Approval Status Checker")
    parser.add_argument("--incident-id", required=True, help="Incident ID")
    parser.add_argument("--action-id", required=True, help="Action ID to check")
    parser.add_argument("--redis-url", required=False, default=None, help="Optional Redis URL override")

    args = parser.parse_args()
    code = asyncio.run(_main_cli(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
