"""Public Twilio ingress with exact-URL signature verification."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from starlette.datastructures import FormData, UploadFile
from twilio.request_validator import RequestValidator  # type: ignore[import-untyped]

from scheduling.domain.sms_ingress import SmsIngressService, normalize_phone
from scheduling.domain.sms_status import SmsDeliveryStatus, SmsStatusStore


def create_twilio_ingress_app(service: SmsIngressService, auth_token: str,
                              inbound_url: str,
                              clock: Callable[[], datetime] | None = None,
                              *, status_url: str | None = None,
                              status_store: SmsStatusStore | None = None,
                              business_id: str | None = None) -> FastAPI:
    """The configured public URL is trusted; proxy Host headers never define the signed URL."""
    parsed = urlsplit(inbound_url)
    if not auth_token or parsed.scheme != "https" or not parsed.netloc or parsed.query:
        raise ValueError("Twilio ingress needs a token and exact HTTPS webhook URL")
    if parsed.path != "/webhooks/sms/inbound":
        raise ValueError("Twilio ingress URL must name the inbound webhook path")
    if (status_url is None) != (status_store is None) or (status_url is None) != (business_id is None):
        raise ValueError("Status URL, store and business ID must be configured together")
    if status_url is not None:
        status_parsed = urlsplit(status_url)
        if (status_parsed.scheme != "https" or not status_parsed.netloc
                or status_parsed.query or status_parsed.path != "/webhooks/sms/status"):
            raise ValueError("Twilio status needs an exact HTTPS webhook URL")
    validator = RequestValidator(auth_token)
    now = clock or (lambda: datetime.now(UTC))
    app = FastAPI(title="Scheduling SMS ingress", version="0.1.0")

    @app.post("/webhooks/sms/inbound")
    async def inbound(request: Request) -> Response:
        fields = await _verified_form(request, validator, inbound_url)
        try:
            service.receive(fields, now())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Advanced Opt-Out owns STOP/HELP replies. A blank response avoids
        # sending duplicate messages or implying any booking state change.
        return Response(status_code=204)

    if status_url is not None and status_store is not None and business_id is not None:
        @app.post("/webhooks/sms/status")
        async def status(request: Request) -> Response:
            fields = await _verified_form(request, validator, status_url)
            try:
                status_store.put_status(SmsDeliveryStatus(
                    business_id, fields.get("MessageSid", ""),
                    fields.get("MessageStatus", ""),
                    normalize_phone(fields.get("To", "")), now(),
                    fields.get("ErrorCode") or None,
                ))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return Response(status_code=204)

    return app


async def _verified_form(request: Request, validator: RequestValidator,
                         public_url: str) -> dict[str, str]:
    if request.url.query or request.headers.get("content-type", "").split(";", 1)[0] != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(status_code=415, detail="Unsupported webhook encoding")
    raw = await request.body()
    if len(raw) > 16_384:
        raise HTTPException(status_code=413, detail="Webhook body is too large")
    try:
        pairs = parse_qsl(raw.decode("utf-8", errors="strict"), keep_blank_values=True,
                          strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Malformed webhook form") from exc
    form = FormData(cast(list[tuple[str, str | UploadFile]], pairs))
    signature = request.headers.get("x-twilio-signature", "")
    if not validator.validate(public_url, form, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    fields: dict[str, str] = {}
    for name, value in pairs:
        if name in fields:
            raise HTTPException(status_code=400, detail="Duplicate webhook field")
        fields[name] = value
    return fields
