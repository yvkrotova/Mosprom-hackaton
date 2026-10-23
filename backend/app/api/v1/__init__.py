"""Публичный API /api/v1."""

from fastapi import APIRouter

from app.api.v1 import events, rules, sources, stats, zones

api_router = APIRouter(prefix="/api/v1")
for module in (sources, zones, rules, events, stats):
    api_router.include_router(module.router)
