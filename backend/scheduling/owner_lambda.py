"""Authenticated owner calendar Lambda entry point for the pilot stack."""

import os

import boto3  # type: ignore[import-untyped]
from mangum import Mangum

from scheduling.owner_auth import create_cognito_owner_app

app = create_cognito_owner_app(
    boto3.client("dynamodb"),
    os.environ["SCHEDULING_TABLE_NAME"],
    os.environ["COGNITO_ISSUER"],
    os.environ["COGNITO_CLIENT_ID"],
    os.environ["OWNER_SUB"],
    os.environ["BUSINESS_ID"],
    ui_domain=os.environ["COGNITO_UI_DOMAIN"],
    ui_redirect_uri=os.environ["OWNER_REDIRECT_URI"],
    cors_origins=(os.environ["OWNER_APP_ORIGIN"],),
)
handler = Mangum(app, lifespan="off")
