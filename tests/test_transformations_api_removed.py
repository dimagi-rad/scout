import pytest
from django.test import Client


@pytest.mark.django_db
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/transformations/assets/"),
        ("post", "/api/transformations/assets/"),
        ("get", "/api/transformations/runs/"),
        ("post", "/api/transformations/runs/trigger/"),
    ],
)
def test_transformations_api_is_gone(user, method, path):
    client = Client()
    client.force_login(user)

    resp = getattr(client, method)(path)

    assert resp.status_code == 404
