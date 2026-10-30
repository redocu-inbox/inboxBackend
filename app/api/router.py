"""Collects every router so main.py stays small as we add endpoints."""

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.graph import router as graph_router
from app.api.forms import router as forms_router


api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(graph_router)
api_router.include_router(forms_router)