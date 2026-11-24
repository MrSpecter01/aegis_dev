import asyncio
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.parse

# Ensure project root is in sys.path when invoked directly
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import httpx
import redis
import typer
import yaml
from rich.console import Console
from rich.table import Table

from shared.config.loader import ConfigManager, get_config
from services.api_server.topology.parser import parse_helm_dir
from services.api_server.topology.store import TopologyStore

try:
    from scripts.seed_runbooks import seed_qdrant_runbooks
except ImportError:
    seed_qdrant_runbooks = None

app = typer.Typer(help="Aegis Incident Response CLI")
seed_app = typer.Typer(help="Seed Aegis knowledge, prompts, and alert rules")
app.add_typer(seed_app, name="seed")

console = Console()


# -----------------------------------------------------------------------------
# aegis init
# -----------------------------------------------------------------------------
@app.command()
def init(
    helm_dir: Optional[str] = typer.Option(
        None,
        "--helm-dir",
        "-h",
        help="Path to directory containing Helm chart manifests.",
    ),
    namespace: str = typer.Option(
        "default",
        "--namespace",
        "-n",
        help="Default namespace for services.",
    ),
):
    """Initialize Aegis topology by parsing Helm charts, building dependency graph,
    and storing it in Redis with a disk snapshot.
    """
    try:
        config = get_config()
    except Exception as e:
        console.print(f"[bold red]Failed to load Aegis configuration:[/bold red] {e}")
        raise typer.Exit(code=1)

    chart_dir = helm_dir or config.system.topology.helm_dir
    snapshot_path = config.system.topology.snapshot_path

    console.print(f"[bold blue]Aegis Topology Init[/bold blue]")
    console.print(f"  Helm Directory: [cyan]{chart_dir}[/cyan]")
    console.print(f"  Namespace:      [cyan]{namespace}[/cyan]")
    console.print(f"  Snapshot Path:  [cyan]{snapshot_path}[/cyan]")

    if not os.path.isdir(chart_dir):
        console.print(f"[bold red]Error:[/bold red] Directory '{chart_dir}' does not exist.")
        raise typer.Exit(code=1)

    nodes, edges = parse_helm_dir(chart_dir, namespace=namespace)

    # Connect to Redis
    try:
        redis_client = redis.from_url(config.system.redis.url, db=config.system.redis.db)
        redis_client.ping()
    except Exception as e:
        console.print(f"[bold yellow]Warning: Could not connect to Redis ({e}). Saving disk snapshot only.[/bold yellow]")
        redis_client = None

    if redis_client:
        store = TopologyStore(redis_client, snapshot_path=snapshot_path)
        store.save(nodes, edges)
    else:
        # Save snapshot directly
        import json
        data = {
            "nodes": [n.model_dump() for n in nodes],
            "edges": [e.model_dump(by_alias=True) for e in edges],
            "graph": {},
        }
        saved = False
        for target_path in [snapshot_path, os.path.abspath("./data/topology_snapshot.json")]:
            try:
                parent_dir = os.path.dirname(target_path)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                with open(target_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                saved = True
                if target_path != snapshot_path:
                    console.print(f"[bold yellow]Note: Could not write to {snapshot_path}. Saved snapshot to {target_path}[/bold yellow]")
                break
            except (PermissionError, OSError):
                continue
        if not saved:
            console.print(f"[bold yellow]Warning: Could not write snapshot file due to filesystem permissions.[/bold yellow]")

    console.print(f"[bold green][OK] Topology initialized successfully![/bold green]")
    console.print(f"  Total Nodes: [bold]{len(nodes)}[/bold]")
    console.print(f"  Total Edges: [bold]{len(edges)}[/bold]")


# -----------------------------------------------------------------------------
# aegis seed runbooks
# -----------------------------------------------------------------------------
@seed_app.command("runbooks")
def seed_runbooks_cmd(
    runbooks_dir: Optional[str] = typer.Option(
        None,
        "--runbooks-dir",
        "-r",
        help="Path to directory containing markdown runbooks.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Enforce strict rule document linting without interactive prompts.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force re-embedding of all runbooks and recreate Qdrant collection if model or dimension changed.",
    ),
    collection: Optional[str] = typer.Option(
        None,
        "--collection",
        "-c",
        help="Target Qdrant collection name (defaults to config.system.qdrant.collection).",
    ),
):
    """Compile runbooks TOC index, generate embeddings, and upsert vectors into Qdrant."""
    console.print("[bold blue]Aegis Seeding: Runbooks & Operational Rules[/bold blue]")

    target_dir = Path(runbooks_dir) if runbooks_dir else Path(project_root) / "seed" / "runbooks"
    if not target_dir.exists():
        console.print(f"[bold red]Error:[/bold red] Runbooks directory '{target_dir}' does not exist.")
        raise typer.Exit(code=1)

    try:
        fn = seed_qdrant_runbooks
        if fn is None:
            from scripts.seed_runbooks import seed_qdrant_runbooks as _fn
            fn = _fn
        count = asyncio.run(
            fn(
                runbooks_dir=target_dir,
                interactive=not strict,
                force=force,
                collection=collection,
            )
        )
        console.print(f"[bold green][OK] Successfully seeded {count} runbook chunk(s) into Qdrant![/bold green]")
    except Exception as exc:
        console.print(f"[bold red]Error seeding runbooks into Qdrant:[/bold red] {exc}")
        raise typer.Exit(code=1)


# -----------------------------------------------------------------------------
# aegis seed prompts
# -----------------------------------------------------------------------------
REQUIRED_PROMPT_FILES = [
    "agents.yaml",
    "tasks.yaml",
    "system_remediation_agent.md",
    "system_scribe_agent.md",
    "incident_report_template.md",
    "triage_summary_template.md",
    "remediation_reasoning_template.md",
    "convergence_eval_template.md",
]


@seed_app.command("prompts")
def seed_prompts_cmd(
    prompts_dir: Optional[str] = typer.Option(
        None,
        "--prompts-dir",
        "-p",
        help="Path to directory containing Aegis agent prompts and templates.",
    ),
):
    """Validate that all required prompt definitions, personas, and templates exist and are valid."""
    console.print("[bold blue]Aegis Seeding: Prompts & Personas Validation[/bold blue]")

    target_dir = Path(prompts_dir) if prompts_dir else Path(project_root) / "seed" / "prompts"
    if not target_dir.exists():
        console.print(f"[bold red]Error:[/bold red] Prompts directory '{target_dir}' does not exist.")
        raise typer.Exit(code=1)

    missing_files: List[str] = []
    validated_files: List[str] = []

    for filename in REQUIRED_PROMPT_FILES:
        filepath = target_dir / filename
        if not filepath.exists():
            missing_files.append(filename)
        else:
            try:
                content = filepath.read_text(encoding="utf-8").strip()
                if not content:
                    missing_files.append(f"{filename} (empty file)")
                else:
                    validated_files.append(filename)
            except Exception as e:
                missing_files.append(f"{filename} (read error: {e})")

    # Validate CrewAI YAML personas and tasks
    agents_file = target_dir / "agents.yaml"
    if agents_file.exists():
        try:
            with open(agents_file, "r", encoding="utf-8") as f:
                agents_data = yaml.safe_load(f) or {}
            for agent_name in ["infrastructure_investigator", "log_trace_investigator", "change_config_investigator"]:
                if agent_name not in agents_data:
                    missing_files.append(f"agents.yaml: missing key '{agent_name}'")
        except Exception as e:
            missing_files.append(f"agents.yaml: parse error ({e})")

    tasks_file = target_dir / "tasks.yaml"
    if tasks_file.exists():
        try:
            with open(tasks_file, "r", encoding="utf-8") as f:
                tasks_data = yaml.safe_load(f) or {}
            for task_name in ["investigate_infrastructure", "investigate_logs_traces", "investigate_changes_configs"]:
                if task_name not in tasks_data:
                    missing_files.append(f"tasks.yaml: missing key '{task_name}'")
        except Exception as e:
            missing_files.append(f"tasks.yaml: parse error ({e})")

    if missing_files:
        console.print(f"[bold red]Error: Missing or invalid required prompt file(s):[/bold red]")
        for item in missing_files:
            console.print(f"  [red][X][/red] {item}")
        raise typer.Exit(code=1)

    console.print(f"[bold green][OK] All {len(validated_files)} prompt templates and agent personas validated successfully![/bold green]")
    for item in validated_files:
        console.print(f"  [green][OK][/green] {item}")


# -----------------------------------------------------------------------------
# aegis seed alert-rules
# -----------------------------------------------------------------------------
@seed_app.command("alert-rules")
def seed_alert_rules_cmd(
    rules_file: Optional[str] = typer.Option(
        None,
        "--rules-file",
        "-f",
        help="Path to Prometheus alerting rules YAML file.",
    ),
    prometheus_url: Optional[str] = typer.Option(
        None,
        "--prometheus-url",
        "-u",
        help="Prometheus management base URL.",
    ),
):
    """Validate Prometheus alerting rules for all chaos scenarios and trigger Prometheus lifecycle reload."""
    console.print("[bold blue]Aegis Seeding: Prometheus Alerting Rules[/bold blue]")

    rules_path = Path(rules_file) if rules_file else Path(project_root) / "seed" / "alert_rules" / "aegis-test-rules.yaml"
    if not rules_path.exists():
        console.print(f"[bold red]Error:[/bold red] Alert rules file '{rules_path}' does not exist.")
        raise typer.Exit(code=1)

    try:
        content = rules_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict) or "groups" not in parsed:
            console.print(f"[bold red]Error:[/bold red] Rules file '{rules_path}' must contain top-level 'groups'.")
            raise typer.Exit(code=1)

        total_rules = 0
        rule_names: List[str] = []
        for grp in parsed.get("groups", []):
            for rule in grp.get("rules", []):
                alert_name = rule.get("alert")
                if alert_name:
                    rule_names.append(alert_name)
                    total_rules += 1

        console.print(f"  Found [cyan]{total_rules}[/cyan] alerting rule(s) in {rules_path.name}:")
        for name in rule_names:
            console.print(f"    - [dim]{name}[/dim]")
    except Exception as exc:
        console.print(f"[bold red]Error parsing alerting rules YAML:[/bold red] {exc}")
        raise typer.Exit(code=1)

    # Resolve Prometheus URL
    try:
        cfg = get_config()
        resolved_url = prometheus_url or cfg.system.mcp_endpoints.prometheus_url or "http://localhost:9090"
    except Exception:
        resolved_url = prometheus_url or "http://localhost:9090"

    clean_url = resolved_url.rstrip("/")
    console.print(f"  Prometheus Target: [cyan]{clean_url}[/cyan]")

    # Trigger Prometheus lifecycle reload: POST /-/reload
    reload_success = False
    try:
        with httpx.Client(timeout=5.0) as client:
            reload_resp = client.post(f"{clean_url}/-/reload")
            if reload_resp.status_code in (200, 204):
                reload_success = True
                console.print(f"  [green][OK][/green] Prometheus configuration reload triggered (HTTP {reload_resp.status_code})")
            else:
                console.print(f"  [yellow]![/yellow] Prometheus reload returned status {reload_resp.status_code}: {reload_resp.text}")

            # Verify active rules via /api/v1/rules
            try:
                rules_resp = client.get(f"{clean_url}/api/v1/rules")
                if rules_resp.status_code == 200:
                    data = rules_resp.json()
                    active_groups = data.get("data", {}).get("groups", [])
                    active_rules_count = sum(len(g.get("rules", [])) for g in active_groups)
                    console.print(f"  [green][OK][/green] Prometheus API confirmed [bold]{active_rules_count}[/bold] active rules across {len(active_groups)} group(s).")
            except Exception as e:
                console.print(f"  [dim yellow]Note: Could not query /api/v1/rules: {e}[/dim yellow]")
    except Exception as exc:
        console.print(f"[bold yellow]Warning: Could not connect to Prometheus at {clean_url}: {exc}[/bold yellow]")
        console.print(f"[dim]Alert rules file is valid locally. In Docker Compose, ensure Prometheus is running with --web.enable-lifecycle.[/dim]")

    console.print(f"[bold green][OK] Alert rules validated successfully ({total_rules} rules)![/bold green]")


# -----------------------------------------------------------------------------
# aegis seed mcp-config
# -----------------------------------------------------------------------------
@seed_app.command("mcp-config")
def seed_mcp_config_cmd():
    """Validate and display registered MCP servers and tools from config/mcp.yaml."""
    console.print("[bold blue]Aegis Seeding: MCP Server Configuration[/bold blue]")
    try:
        cfg = ConfigManager()
        servers = cfg.mcp.mcp_servers
    except Exception as exc:
        console.print(f"[bold red]Error loading mcp.yaml:[/bold red] {exc}")
        raise typer.Exit(code=1)

    table = Table(title="Configured MCP Servers & Tools")
    table.add_column("Server", style="cyan")
    table.add_column("Server URL", style="magenta")
    table.add_column("Registered Tools", style="green")

    total_tools = 0
    for name, server in servers.items():
        tools_str = ", ".join(server.tool_names)
        total_tools += len(server.tools)
        target_display = (
            server.server_url
            if server.transport != "stdio"
            else f"stdio://{server.command} {' '.join(server.args)}".strip()
        )
        table.add_row(name, target_display, tools_str)

    console.print(table)
    console.print(f"[bold green][OK] Registered {len(servers)} MCP servers with {total_tools} tools total![/bold green]")


# -----------------------------------------------------------------------------
# aegis verify
# -----------------------------------------------------------------------------
@app.command()
def verify():
    """Execute live connectivity checks against Redis, Qdrant, Ollama, and all configured MCP servers."""
    console.print("[bold blue]Aegis System Health & Connectivity Verification[/bold blue]")

    try:
        cfg = ConfigManager()
    except Exception as exc:
        console.print(f"[bold red]Failed to load configuration:[/bold red] {exc}")
        raise typer.Exit(code=1)

    table = Table(title="Aegis Component Health Verification")
    table.add_column("Component", style="cyan", no_wrap=True)
    table.add_column("Type", style="dim")
    table.add_column("Target URL / Address", style="magenta")
    table.add_column("Status", justify="center")
    table.add_column("Details", style="dim")

    overall_pass = True

    # 1. Redis Check
    redis_url = cfg.system.redis.url
    try:
        t0 = time.perf_counter()
        r = redis.from_url(redis_url, db=cfg.system.redis.db, socket_timeout=3.0)
        r.ping()
        latency = (time.perf_counter() - t0) * 1000
        table.add_row("Redis", "State / Queue Store", redis_url, "[green]PASS[/green]", f"{latency:.1f}ms latency")
    except Exception as e:
        overall_pass = False
        table.add_row("Redis", "State / Queue Store", redis_url, "[red]FAIL[/red]", str(e)[:60])

    # 2. Qdrant Vector DB Check
    qdrant_url = cfg.system.qdrant.url.rstrip("/")
    try:
        t0 = time.perf_counter()
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(f"{qdrant_url}/collections")
            latency = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                colls = resp.json().get("result", {}).get("collections", [])
                table.add_row("Qdrant", "Vector Database", qdrant_url, "[green]PASS[/green]", f"{len(colls)} collection(s), {latency:.1f}ms")
            else:
                overall_pass = False
                table.add_row("Qdrant", "Vector Database", qdrant_url, "[red]FAIL[/red]", f"HTTP {resp.status_code}")
    except Exception as e:
        overall_pass = False
        table.add_row("Qdrant", "Vector Database", qdrant_url, "[red]FAIL[/red]", str(e)[:60])

    # 3. Ollama LLM Serving Check
    ollama_url = cfg.system.ollama.url.rstrip("/")
    try:
        t0 = time.perf_counter()
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(f"{ollama_url}/api/tags")
            latency = (time.perf_counter() - t0) * 1000
            if resp.status_code == 200:
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                table.add_row("Ollama", "Local LLM Serving", ollama_url, "[green]PASS[/green]", f"{len(models)} model(s) available, {latency:.1f}ms")
            else:
                overall_pass = False
                table.add_row("Ollama", "Local LLM Serving", ollama_url, "[red]FAIL[/red]", f"HTTP {resp.status_code}")
    except Exception as e:
        overall_pass = False
        table.add_row("Ollama", "Local LLM Serving", ollama_url, "[red]FAIL[/red]", str(e)[:60])

    # 4. MCP Servers Check
    for name, srv in cfg.mcp.mcp_servers.items():
        if srv.transport == "stdio":
            cmd_str = f"stdio://{srv.command} {' '.join(srv.args)}".strip()
            if srv.command and shutil.which(srv.command):
                table.add_row(f"MCP: {name}", "STDIO Tool Server", cmd_str, "[green]PASS[/green]", f"executable resolved (tool count: {len(srv.tools)})")
            else:
                overall_pass = False
                table.add_row(f"MCP: {name}", "STDIO Tool Server", cmd_str, "[red]FAIL[/red]", f"command not found: {srv.command}")
            continue

        parsed_url = urllib.parse.urlparse(srv.server_url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        health_url = f"{base_url}/health"

        try:
            t0 = time.perf_counter()
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(health_url)
                latency = (time.perf_counter() - t0) * 1000
                if resp.status_code == 200:
                    table.add_row(f"MCP: {name}", "Tool Server", srv.server_url, "[green]PASS[/green]", f"{latency:.1f}ms (tool count: {len(srv.tools)})")
                else:
                    overall_pass = False
                    table.add_row(f"MCP: {name}", "Tool Server", srv.server_url, "[red]FAIL[/red]", f"HTTP {resp.status_code}")
        except Exception as e:
            overall_pass = False
            table.add_row(f"MCP: {name}", "Tool Server", srv.server_url, "[red]FAIL[/red]", str(e)[:60])

    console.print(table)

    if overall_pass:
        console.print("[bold green][OK] All Aegis services verified and ready to respond![/bold green]")
    else:
        console.print("[bold red][FAIL] One or more Aegis components failed health verification.[/bold red]")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
