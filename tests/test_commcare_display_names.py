"""Display labels must not prevent CommCare form staging from being generated."""

import re

import pytest
from django.db import connection

from apps.transformations.services.commcare_staging import generate_system_assets
from apps.users.models import Tenant


@pytest.fixture
def tenant():
    return Tenant(provider="commcare", external_id="synthetic-display-names")


@pytest.mark.parametrize("label", ['">  ', "---", "", "   ", "日本語", "📝", {"ja": "日本語"}])
def test_non_slugifiable_form_labels_have_safe_model_names(tenant, label):
    assets = generate_system_assets(
        tenant, {"form_definitions": {"urn:synthetic:form": {"name": label, "questions": []}}}
    )
    assert len(assets) == 1
    assert re.fullmatch(r"stg_form_unnamed_[0-9a-f]{8}", assets[0].name)
    assert "WHERE xmlns = 'urn:synthetic:form'" in assets[0].sql_content


def test_fallback_identity_is_stable_and_distinct_for_each_form(tenant):
    def names(forms):
        return {
            asset.sql_content: asset.name
            for asset in generate_system_assets(
                tenant,
                {
                    "form_definitions": forms,
                },
            )
        }

    forms = {
        "urn:synthetic:a": {"name": "---", "questions": []},
        "urn:synthetic:b": {"name": "---", "questions": []},
    }
    first = names(forms)
    assert len(set(first.values())) == 2
    assert first == names(dict(reversed(list(forms.items()))))
    forms["urn:synthetic:a"]["name"] = "日本語"
    assert first == names(forms)


@pytest.mark.parametrize("app_name", ['">  ', "日本語"])
def test_duplicate_forms_with_non_slugifiable_app_label_stay_unique(tenant, app_name):
    forms = {
        f"urn:synthetic:{index}": {
            "name": "Registration",
            "app_name": app_name,
            "app_id": "synthetic-app",
            "questions": [],
        }
        for index in range(3)
    }
    assets = generate_system_assets(tenant, {"form_definitions": forms})
    assert assets[0].name == "stg_form_registration"
    assert len({asset.name for asset in assets}) == 3
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", asset.name) for asset in assets)


def test_repeat_group_references_fallback_parent(tenant):
    assets = generate_system_assets(
        tenant,
        {
            "form_definitions": {
                "urn:synthetic:repeat": {
                    "name": "---",
                    "questions": [
                        {
                            "value": "/data/children/name",
                            "repeat": "/data/children",
                            "type": "Text",
                        },
                    ],
                },
            }
        },
    )
    parent, repeat = assets
    assert re.fullmatch(r"stg_form_unnamed_[0-9a-f]{8}", parent.name)
    assert f"ref('{parent.name}')" in repeat.sql_content


@pytest.mark.django_db
def test_fallback_form_sql_reads_the_original_xmlns(tenant):
    xmlns = "urn:synthetic:form'one"
    asset = generate_system_assets(
        tenant,
        {
            "form_definitions": {
                xmlns: {"name": '">  ', "questions": [{"value": "/data/name", "type": "Text"}]},
            }
        },
    )[0]
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TEMP TABLE raw_forms (form_id text, xmlns text, received_on text, "
            "app_id text, form_data jsonb) ON COMMIT DROP"
        )
        cursor.execute(
            "INSERT INTO raw_forms VALUES (%s, %s, %s, %s, %s)",
            [
                "synthetic-id",
                xmlns,
                "2026-09-01",
                "synthetic-app",
                '{"data":{"name":"Example"}}',
            ],
        )
        cursor.execute(asset.sql_content)
        rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "synthetic-id"
    assert rows[0][-1] == "Example"
