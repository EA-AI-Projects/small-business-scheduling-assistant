"""Print the owner API OpenAPI schema for frontend type generation.

Usage: python -m scheduling.owner_openapi > frontend/openapi/owner.json
"""

import json
import sys

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.adapters.sms_dynamodb import DynamoSmsIngressStore
from scheduling.owner_api import OwnerPrincipal, create_owner_app


def owner_schema() -> dict[str, object]:
    def reject(token: str) -> OwnerPrincipal:
        del token
        raise ValueError("Schema export never authenticates")

    # The deployed app always mounts the SMS consent and delivery-failure routes, so the
    # exported contract must include them. The store is never called while exporting.
    sms_store = DynamoSmsIngressStore(None, "schema-export")  # type: ignore[arg-type]
    return create_owner_app(InMemoryCalendarRepository(), reject, sms_store=sms_store).openapi()


def main() -> None:
    json.dump(owner_schema(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
