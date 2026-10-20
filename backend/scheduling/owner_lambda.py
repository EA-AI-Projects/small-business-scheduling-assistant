"""Authenticated owner calendar Lambda entry point for the pilot stack."""

import os

import boto3  # type: ignore[import-untyped]
from mangum import Mangum

from scheduling.adapters.openai_messages import OpenAIMessageInterpreter
from scheduling.linked_auth import create_cognito_linked_app


def _preference_interpreter() -> OpenAIMessageInterpreter | None:
    """Preference review is a model call; without the key parameter the route reports it off."""
    name = os.environ.get("OPENAI_API_KEY_PARAM")
    if not name:
        return None
    if not name.startswith("/scheduling/"):
        raise ValueError("OpenAI key parameter must be under /scheduling/")
    parameter = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True).get("Parameter", {})
    if parameter.get("Type") != "SecureString" or not parameter.get("Value"):
        raise ValueError("OpenAI key parameter must be a nonempty SecureString")
    return OpenAIMessageInterpreter(str(parameter["Value"]), timeout_seconds=20)


app = create_cognito_linked_app(
    boto3.client("dynamodb"),
    os.environ["SCHEDULING_TABLE_NAME"],
    os.environ["COGNITO_ISSUER"],
    os.environ["COGNITO_CLIENT_ID"],
    cors_origins=(os.environ["OWNER_APP_ORIGIN"],),
    cognito_client=boto3.client("cognito-idp"),
    preference_interpreter=_preference_interpreter(),
)
handler = Mangum(app, lifespan="off")
