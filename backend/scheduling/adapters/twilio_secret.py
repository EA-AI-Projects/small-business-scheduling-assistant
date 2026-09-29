"""Read the Twilio token from a named encrypted SSM parameter at cold start."""

from typing import Any


def load_twilio_auth_token(ssm_client: Any, parameter_name: str) -> str:
    if not parameter_name.startswith("/scheduling/"):
        raise ValueError("Twilio token parameter must be under /scheduling/")
    response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
    parameter = response.get("Parameter", {})
    if parameter.get("Type") != "SecureString":
        raise ValueError("Twilio token parameter must be SecureString")
    token = parameter.get("Value", "")
    if not token:
        raise ValueError("Twilio token parameter is empty")
    return str(token)
