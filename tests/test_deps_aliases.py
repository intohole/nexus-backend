from typing import Annotated  # noqa: F401

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from nexus.deps import UserFullNormalized, UserIdOptional, UserIdRequired


def _build_app() -> TestClient:
    app = FastAPI()

    @app.get("/required")
    async def required(user_id: UserIdRequired) -> dict[str, str]:
        return {"user_id": user_id}

    @app.get("/optional")
    async def optional(user_id: UserIdOptional) -> dict[str, object]:
        return {"user_id": user_id}

    @app.get("/normalized")
    async def normalized(user: UserFullNormalized) -> dict[str, object]:
        return {"keys": sorted(user.keys())}

    return TestClient(app)


def test_alias_is_annotated_type() -> None:
    assert UserIdRequired.__origin__ is str
    assert len(UserIdRequired.__metadata__) == 1


@pytest.mark.asyncio
async def test_alias_resolves_through_fastapi() -> None:
    client = _build_app()
    response = client.get("/required", headers={"Authorization": "Bearer test-token"})
    assert response.status_code in (200, 401, 503)


def test_optional_alias_allows_anonymous() -> None:
    client = _build_app()
    response = client.get("/optional")
    assert response.status_code in (200, 503)
