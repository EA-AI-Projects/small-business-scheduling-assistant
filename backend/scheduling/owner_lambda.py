"""Authenticated owner calendar Lambda entry point for the pilot stack."""

import os

import boto3  # type: ignore[import-untyped]
from mangum import Mangum

from scheduling.linked_auth import create_cognito_linked_app

app = create_cognito_linked_app(
    boto3.client("dynamodb"),
    os.environ["SCHEDULING_TABLE_NAME"],
    os.environ["COGNITO_ISSUER"],
    os.environ["COGNITO_CLIENT_ID"],
    cors_origins=(os.environ["OWNER_APP_ORIGIN"],),
)
handler = Mangum(app, lifespan="off")
