"""Liveness: the process is up and answering.

Deliberately not a readiness check. It touches no database, provider or Telegram,
so an orchestrator that restarts a process on a failing liveness probe cannot be
tricked into restart loops by a database outage. It needs no authentication and
discloses nothing about the deployment.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
