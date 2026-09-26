"""FastAPI boundary; scheduling rules remain in the domain service."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.domain.availability import AvailabilityRepository, AvailabilityService
from scheduling.domain.calendar import CalendarService, CalendarStatus


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    start_at: datetime
    end_at: datetime
    status: CalendarStatus
    hold_expires_at: datetime | None
    buffer_minutes: int


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


class AvailabilityQueryRequest(BaseModel):
    day: date
    duration_minutes: int = Field(gt=0)


class AvailabilityResponse(BaseModel):
    business_id: str
    day: date
    starts_at: list[datetime]


def create_app(
    repository: AvailabilityRepository | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    app = FastAPI(title="Small Business Scheduling API", version="0.1.0")
    store = repository if repository is not None else InMemoryCalendarRepository()
    calendar_service = CalendarService(store)
    availability_service = AvailabilityService(store)
    now = clock or (lambda: datetime.now(UTC))

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/v1/businesses/{business_id}/calendar", response_model=CalendarResponse)
    def calendar(
        business_id: str, query: Annotated[CalendarQueryRequest, Query()]
    ) -> CalendarResponse:
        snapshot = calendar_service.get_calendar(business_id, query.start_at, query.end_at, now())
        return CalendarResponse(
            business_id=snapshot.business_id,
            revision=snapshot.revision,
            events=[EventResponse.model_validate(event) for event in snapshot.events],
        )

    @app.get("/v1/businesses/{business_id}/availability", response_model=AvailabilityResponse)
    def availability(
        business_id: str, query: Annotated[AvailabilityQueryRequest, Query()]
    ) -> AvailabilityResponse:
        starts = availability_service.find_starts(
            business_id, query.day, query.duration_minutes, now()
        )
        return AvailabilityResponse(
            business_id=business_id, day=query.day, starts_at=list(starts)
        )

    return app


app = create_app()
