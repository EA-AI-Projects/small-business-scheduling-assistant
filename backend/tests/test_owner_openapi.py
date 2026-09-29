"""The exported owner schema feeds frontend request types."""

from scheduling.owner_openapi import owner_schema


def test_owner_schema_lists_owner_routes_and_request_bodies() -> None:
    schema = owner_schema()
    paths = schema["paths"]
    assert isinstance(paths, dict)
    assert "/v1/owner/businesses/{business_id}/calendar" in paths
    assert not any(path.startswith("/owner") for path in paths)
    components = schema["components"]
    assert isinstance(components, dict)
    for name in ("ClientProfileBody", "BlockBody", "PolicyEditBody", "DecisionBody"):
        assert name in components["schemas"]
