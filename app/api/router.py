"""Collects every router so main.py stays small as we add endpoints."""

from fastapi import APIRouter

from app.api.health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router)
