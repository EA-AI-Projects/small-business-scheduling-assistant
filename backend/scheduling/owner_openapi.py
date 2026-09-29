"""Print the owner API OpenAPI schema for frontend type generation.

Usage: python -m scheduling.owner_openapi > frontend/openapi/owner.json
"""

import json
import sys

from scheduling.adapters.memory import InMemoryCalendarRepository
from scheduling.owner_api import OwnerPrincipal, create_owner_app


def owner_schema() -> dict[str, object]:
    def reject(_token: str) -> OwnerPrincipal:
        raise ValueError("Schema export never authenticates")

    return create_owner_app(InMemoryCalendarRepository(), reject).openapi()


def main() -> None:
    json.dump(owner_schema(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
