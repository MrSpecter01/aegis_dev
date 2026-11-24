import asyncio
import contextlib
import logging
import signal
import redis
from typing import Optional
from fastapi import FastAPI
from shared.audit.tracker import set_audit_redis_client
from shared.auth.registry import IdentityRegistry
from shared.config.loader import ConfigManager

logger = logging.getLogger("aegis.api_server")

from services.api_server.middleware import APIKeyMiddleware
from services.api_server.routers.incidents import router as incidents_router
from services.api_server.routers.config_routes import router as config_router
from services.api_server.routers.drift import router as drift_router
from services.api_server.routers.system import router as system_router
from services.api_server.routers.topology import router as topology_router
from services.api_server.topology.store import TopologyStore
from services.api_server.ingestion.consumer import run_alert_consumer

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    config = getattr(app.state, "config", None) or ConfigManager()
    redis_url = config.system.redis.url
    
    app.state.redis = redis.from_url(redis_url, decode_responses=False)
    set_audit_redis_client(app.state.redis)
    
    snapshot_path = "/data/topology_snapshot.json"
    if hasattr(config.system, "topology") and hasattr(config.system.topology, "snapshot_path"):
        snapshot_path = config.system.topology.snapshot_path

    topology_store = TopologyStore(app.state.redis, snapshot_path)
    app.state.topology_store = topology_store
    app.state.config = config

    # Topology lifecycle check & auto-discovery
    monitored_namespaces = ["boutique", "default"]
    if hasattr(config.system, "topology") and hasattr(config.system.topology, "monitored_namespaces"):
        monitored_namespaces = config.system.topology.monitored_namespaces or monitored_namespaces

    from services.api_server.topology.sync import sync_topology_sources

    existing_nodes = topology_store.all_nodes()
    existing_namespaces = {n.namespace for n in existing_nodes if n.namespace}
    if not existing_nodes or not set(monitored_namespaces).intersection(existing_namespaces):
        logger.info(
            "[TOPOLOGY BOOT] Initializing/syncing topology store (existing namespaces: %s)...",
            sorted(list(existing_namespaces)),
        )
        try:
            await sync_topology_sources(topology_store, config)
            existing_nodes = topology_store.all_nodes()
            logger.info(
                "[TOPOLOGY BOOT] Sync completed: %d total services loaded across namespaces: %s",
                len(existing_nodes),
                sorted(list({n.namespace for n in existing_nodes if n.namespace})),
            )
        except Exception as exc:
            logger.warning("[TOPOLOGY BOOT] Topology sync failed: %s", exc)
    else:
        node_names = [n.id for n in existing_nodes]
        logger.info(
            "[TOPOLOGY BOOT] Topology ready. Loaded %d services: %s",
            len(existing_nodes),
            ", ".join(node_names[:10]) + ("..." if len(node_names) > 10 else ""),
        )
        # Operator-gated SHA-256 drift detection (alerts operator without overwriting)
        try:
            from services.api_server.topology.sync import detect_topology_drift
            from scripts.seed_runbooks import detect_runbook_drift

            topo_drift = await detect_topology_drift(topology_store, config, app.state.redis, notify=True)
            if topo_drift:
                logger.warning(
                    "[TOPOLOGY DRIFT CHECK] Pending drift detected (%s -> %s); awaiting operator approval.",
                    str(topo_drift.get("old_sha", ""))[:12],
                    str(topo_drift.get("new_sha", ""))[:12],
                )
            rb_drift = await asyncio.to_thread(detect_runbook_drift, None, app.state.redis, None, config.system, True)
            if rb_drift:
                logger.warning(
                    "[RUNBOOK DRIFT CHECK] Pending drift detected (%s -> %s); awaiting operator approval.",
                    str(rb_drift.get("old_sha", ""))[:12],
                    str(rb_drift.get("new_sha", ""))[:12],
                )
        except Exception as exc:
            logger.debug("[DRIFT CHECK] Startup drift check deferred: %s", exc)

    consumer_task = None
    if hasattr(config.system, "ingestion"):
        consumer_task = asyncio.create_task(
            run_alert_consumer(app.state.redis, config.system, topology_store)
        )
        app.state.consumer_task = consumer_task

    from services.api_server.sag.approval import rearm_pending_approval_timers
    from services.api_server.incident_store import run_idle_incident_archiver
    sweep_task = asyncio.create_task(rearm_pending_approval_timers(app.state.redis))
    archiver_task = asyncio.create_task(
        run_idle_incident_archiver(app.state.redis, config.system.ledger)
    )

    yield

    if consumer_task:
        consumer_task.cancel()
        await asyncio.gather(consumer_task, return_exceptions=True)

    if sweep_task:
        sweep_task.cancel()
        await asyncio.gather(sweep_task, return_exceptions=True)

    if archiver_task:
        archiver_task.cancel()
        await asyncio.gather(archiver_task, return_exceptions=True)

    if hasattr(app.state, "redis"):
        app.state.redis.close()

from fastapi.middleware.cors import CORSMiddleware

def create_app(
    config_dir: str = None,
    internal_mode: bool = False,
    shared_app: Optional[FastAPI] = None,
) -> FastAPI:
    title = "Aegis Internal Control Plane API" if internal_mode else "Aegis Control Plane API"
    app = FastAPI(title=title, lifespan=None if shared_app is not None else lifespan)

    if shared_app is not None:
        app.state = shared_app.state
        config = shared_app.state.config
        registry = shared_app.state.identity_registry
    else:
        config = ConfigManager(config_dir)
        app.state.config = config
        registry = IdentityRegistry.from_config(
            getattr(config, "identities", None),
            legacy_api_key=getattr(getattr(config.system, "system", None), "api_key", ""),
        )
        app.state.identity_registry = registry

    app.add_middleware(
        APIKeyMiddleware,
        api_key=config.system.system.api_key,
        registry=registry,
        internal_mode=internal_mode,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(system_router, prefix="/v1")
    app.include_router(config_router, prefix="/v1/config")
    app.include_router(incidents_router, prefix="/v1")
    app.include_router(topology_router, prefix="/v1")
    app.include_router(drift_router, prefix="/v1")

    return app


async def _serve_dual_ports() -> None:
    import uvicorn

    config = ConfigManager()
    ext_port = int(getattr(config.system.system, "api_port", 8080))
    int_port = int(getattr(config.system.system, "internal_api_port", 8081))

    ext_app = create_app(internal_mode=False)
    int_app = create_app(internal_mode=True, shared_app=ext_app)

    ext_server = uvicorn.Server(uvicorn.Config(ext_app, host="0.0.0.0", port=ext_port, log_level="info"))
    int_server = uvicorn.Server(uvicorn.Config(int_app, host="0.0.0.0", port=int_port, log_level="info"))

    def _request_shutdown(*_args):
        ext_server.should_exit = True
        int_server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_shutdown)
        except Exception:
            pass

    logger.info("Starting Aegis API servers: external=0.0.0.0:%d, internal=0.0.0.0:%d", ext_port, int_port)
    await asyncio.gather(ext_server.serve(), int_server.serve())


if __name__ == "__main__":
    asyncio.run(_serve_dual_ports())
