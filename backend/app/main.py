"""Application entry point.

The implementation lives across the ``app`` package modules (config/database/
schemas/probes/notifications/pipeline/routers). This module wires the FastAPI
application together and — importantly for the test suite — re-exports every
public name, so ``import app.main as m; monkeypatch.setattr(m, ...)`` keeps
working exactly as before the split.

Patching semantics (do not "optimize" these away):
- Names re-exported below resolve to THIS module's binding. Pipeline code looks
  them up via ``app.main`` at call time, so patches take effect.
- ``db``, ``time`` and ``logger`` are shared objects: patching ``m.time.monotonic``
  or ``m.logger.propagate`` mutates them for every module that holds the same
  reference.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import *  # noqa: F401,F403 -- re-exported for the patch surface
from app.database import db, hash_password, init_db, now, verify_password  # noqa: F401
from app.notifications import notify, send_email  # noqa: F401
from app.pipeline import (check_due_devices, metrics_retention_loop,  # noqa: F401
                          monitor_loop, persist_result, run_device_check,
                          scheduler_loop, sweep_old_metrics)
from app.probes import (get_ssl_days_left, numeric_snmp,  # noqa: F401
                        perform_check, snmp_get, socket_create_connection)
from app.routers import (current_user, require_admin,  # noqa: F401
                         router, validate_config)
from app.schemas import *  # noqa: F401,F403 -- re-exported for the patch surface

import asyncio
import ssl
import subprocess
import time  # noqa: F401 -- tests patch m.time.monotonic; scheduler reads main.time

import httpx  # noqa: F401 -- tests patch m.httpx.AsyncClient; probes import httpx directly

# --- logging (same behaviour as the pre-split module) ------------------------
logger = logging.getLogger("wwnm")
logger.setLevel(LOG_LEVEL)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

# --- application -------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    init_db()
    app.state.monitor_task = asyncio.create_task(monitor_loop())
    app.state.retention_task = asyncio.create_task(metrics_retention_loop())
    try:
        yield
    finally:
        for task in (app.state.monitor_task, app.state.retention_task):
            task.cancel()
        # Wait for the loops to actually finish so shutdown is deterministic.
        await asyncio.gather(app.state.monitor_task, app.state.retention_task, return_exceptions=True)


app = FastAPI(title="WinsWiew Network Monitor API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])
app.include_router(router)
