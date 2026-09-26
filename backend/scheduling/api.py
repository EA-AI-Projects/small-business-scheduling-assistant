"""FastAPI boundary; scheduling rules remain in the domain service."""

from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, model_validator

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.calendar import CalendarRepository, CalendarService, CalendarStatus


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    start_at: datetime
    end_at: datetime
    status: CalendarStatus
    hold_expires_at: datetime | None


class CalendarResponse(BaseModel):
    business_id: str
    revision: int
    events: list[EventResponse]


class CalendarQueryRequest(BaseModel):
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "CalendarQueryRequest":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("Calendar query instants must include UTC offsets")
        if self.end_at <= self.start_at:
            raise ValueError("Calendar query end must follow start")
        return self


class HealthResponse(BaseModel):
    status: str


def create_app(repository: CalendarRepository | None = None) -> FastAPI:
    app = FastAPI(title="Small Business Scheduling API", version="0.1.0")
    service = CalendarService(repository if repository is not None else InMemoryCalendarRepository())

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/v1/businesses/{business_id}/calendar", response_model=CalendarResponse)
    def calendar(
        business_id: str, query: Annotated[CalendarQueryRequest, Query()]
    ) -> CalendarResponse:
        snapshot = service.get_calendar(business_id, query.start_at, query.end_at)
        return CalendarResponse(
            business_id=snapshot.business_id,
            revision=snapshot.revision,
            events=[EventResponse.model_validate(event) for event in snapshot.events],
        )

    return app


app = create_app()
