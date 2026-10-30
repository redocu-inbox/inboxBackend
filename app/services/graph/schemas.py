"""Pydantic models for Microsoft Graph request and response payloads."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class GraphChangeNotification(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscription_id: str = Field(alias="subscriptionId")
    client_state: str = Field(alias="clientState")
    change_type: str = Field(alias="changeType")
    resource: str


class GraphNotificationPayload(BaseModel):
    value: list[GraphChangeNotification]


class DeltaMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    subject: str | None = None
    body_preview: str | None = Field(default=None, alias="bodyPreview")
    received_date_time: datetime | None = Field(default=None, alias="receivedDateTime")


class DeltaPage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    value: list[DeltaMessage] = Field(default_factory=list)
    delta_link: str | None = Field(default=None, alias="@odata.deltaLink")
    next_link: str | None = Field(default=None, alias="@odata.nextLink")
