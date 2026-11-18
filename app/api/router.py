"""Collects every router so main.py stays small as we add endpoints."""

from fastapi import APIRouter

from app.api.approvals import router as approvals_router
from app.api.attachments import router as attachments_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.graph import router as graph_router
from app.api.forms import router as forms_router
from app.api.users import router as users_router
from app.api.messages import router as messages_router
from app.api.slack import router as slack_router


api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(graph_router)
api_router.include_router(forms_router)
api_router.include_router(messages_router)
api_router.include_router(slack_router)
api_router.include_router(approvals_router)
api_router.include_router(attachments_router)
