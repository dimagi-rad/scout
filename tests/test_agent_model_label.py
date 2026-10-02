import pytest
from django.test import Client

from apps.agents.model_label import model_display_name


@pytest.mark.parametrize(
    ("model_id", "label"),
    [
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-sonnet-5-5", "Sonnet 5.5"),
        ("claude-haiku-4-5", "Haiku 4.5"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("claude-opus-5-5-latest", "Opus 5.5"),
        ("some-custom-model", "some-custom-model"),
        ("claude-opus-5", "claude-opus-5"),
    ],
)
def test_model_display_name(model_id, label):
    assert model_display_name(model_id) == label


@pytest.mark.django_db
def test_me_exposes_configured_agent_model(user, settings):
    settings.DEFAULT_LLM_MODEL = "claude-sonnet-5-5"
    client = Client()
    client.force_login(user)

    body = client.get("/api/auth/me/").json()

    assert body["agent_model"] == {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5"}
