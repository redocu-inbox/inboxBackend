"""Webhook route for Microsoft Graph change notifications.

Mailbox connect is `GET /auth/microsoft/start`. This route handles the
subscription handshake and change notifications.
"""

import logging

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.api.deps import get_arq
from app.config import get_settings
from app.services.graph.schemas import GraphNotificationPayload

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhooks/graph")
async def graph_webhook(
    request: Request,
    validation_token: str | None = Query(default=None, alias="validationToken")
) -> Response:
    """Accept a Graph subscription handshake, or enqueue a delta sync.

    Graph sends `validationToken` when a subscription is created. The response
    body must be that exact token as text/plain. Other calls are change
    notifications. A matching clientState enqueues sync_mailbox_delta.
    """
    if validation_token is not None:
        return PlainTextResponse(validation_token)

    payload = GraphNotificationPayload.model_validate(await request.json())
    settings = get_settings()
    arq = get_arq(request)
    for item in payload.value:
        if item.client_state != settings.graph_webhook_client_state:
            logger.warning(
                "Graph webhook client_state did not match subscription %s",
                item.subscription_id,
            )
            continue
        await arq.enqueue_job("sync_mailbox_delta", item.subscription_id)
    return Response(status_code=status.HTTP_202_ACCEPTED)