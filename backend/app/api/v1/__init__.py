"""Публичный API /api/v1."""

from fastapi import APIRouter

from app.api.v1 import analytics, events, rules, shifts, sources, stats, zones

api_router = APIRouter(prefix="/api/v1")
for module in (sources, zones, rules, events, stats, shifts, analytics):
    api_router.include_router(module.router)
