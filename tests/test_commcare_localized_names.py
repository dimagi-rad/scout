"""Localized ``{"en": ...}`` names must not sink a tenant's staging layer.

SCOUT-DJANGO-3D: the metadata loader stores app names and module case types
raw, and CommCare returns some of them as translation dicts. PR #466's digest
fallback catches only ValueError, so a dict reached ``str.lower`` and raised
AttributeError out of generation; the materializer's per-tenant catch then
dropped every staging asset for the tenant while the run completed green.
"""

import re

import pytest

from apps.common.localized import localized_str
from apps.transformations.services.commcare_staging import generate_system_assets
from apps.transformations.services.connect_staging import generate_connect_assets
from apps.users.models import Tenant
from apps.workspaces.api import views

DIGEST_NAME = r"unnamed_[0-9a-f]{8}"


@pytest.fixture
def tenant():
    return Tenant(provider="commcare", external_id="synthetic-localized-names")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Patient", "Patient"),
        ({"en": "Patient", "fr": "Patient(e)"}, "Patient"),
        ({"fr": "Ménage"}, "Ménage"),
        ({"en": "", "fr": "Ménage"}, "Ménage"),
        ({"en": {"nested": "Deep"}}, "Deep"),
        ({"en": None, "fr": None}, ""),
        ({}, ""),
        (None, ""),
        (5, ""),
        (["Patient"], ""),
    ],
)
def test_localized_str_unwraps_translations_and_rejects_non_strings(value, expected):
    assert localized_str(value) == expected


def test_views_share_the_common_helper():
    assert views.localized_str is localized_str
    assert not hasattr(views, "_localized_str")


def test_localized_case_type_generates_a_model(tenant):
    metadata = {
        "case_types": [{"name": {"en": "Patient"}, "app_name": {"en": "Clinic"}}],
        "app_definitions": [
            {
                "modules": [
                    {
                        "case_type": {"en": "Patient"},
                        "case_properties": [{"key": {"en": "dob"}}, {"key": "name"}],
                    }
                ]
            }
        ],
    }
    (asset,) = generate_system_assets(tenant, metadata)
    assert asset.name == "stg_case_patient"
    assert "WHERE case_type = 'Patient'" in asset.sql_content
    assert "properties->>'dob' AS \"dob\"" in asset.sql_content
    assert "properties->>'name' AS \"name\"" in asset.sql_content
    assert asset.description == "Staging model for Patient cases"


def test_duplicate_forms_with_localized_app_name_stay_unique(tenant):
    forms = {
        f"urn:synthetic:{index}": {
            "name": "Registration",
            "app_name": {"en": "Maternal App"},
            "app_id": "synthetic-app",
            "questions": [],
        }
        for index in range(2)
    }
    names = [a.name for a in generate_system_assets(tenant, {"form_definitions": forms})]
    assert names == ["stg_form_registration", "stg_form_registration_maternal_app_1"]


def test_localized_form_name_names_the_model_and_description(tenant):
    (asset,) = generate_system_assets(
        tenant,
        {
            "form_definitions": {
                "urn:synthetic:form": {"name": {"fr": "Ménage", "en": "Household"}, "questions": []}
            }
        },
    )
    assert asset.name == "stg_form_household"
    assert asset.description == "Staging model for form: Household"


def test_unusable_localized_values_degrade_without_raising(tenant):
    metadata = {
        "case_types": [{"name": {"en": ""}}, {"name": {"en": None}}, {"name": {"en": "---"}}],
        "form_definitions": {
            f"urn:synthetic:{index}": {
                "name": {"en": None},
                "app_name": {"en": 5},
                "app_id": None,
                "questions": [],
            }
            for index in range(2)
        },
    }
    case_name, *form_names = [a.name for a in generate_system_assets(tenant, metadata)]
    assert re.fullmatch(rf"stg_case_{DIGEST_NAME}", case_name)
    assert all(re.fullmatch(rf"stg_form_{DIGEST_NAME}", name) for name in form_names)
    # Each form's digest keys on its own xmlns, so two unnamed forms never collide.
    assert len(set(form_names)) == 2


def test_non_string_question_paths_are_skipped(tenant):
    (asset,) = generate_system_assets(
        tenant,
        {
            "form_definitions": {
                "urn:synthetic:form": {
                    "name": "Reg",
                    "questions": [
                        {"value": {"en": "/data/bad"}, "type": "Text"},
                        {"value": "/data/name", "type": "Text"},
                        {"value": "/data/child", "repeat": {"en": "/data/group"}},
                    ],
                }
            }
        },
    )
    assert asset.name == "stg_form_reg"
    assert 'AS "name"' in asset.sql_content
    assert "bad" not in asset.sql_content
    assert "child" not in asset.sql_content


def test_connect_non_string_question_paths_are_skipped(tenant):
    tenant.provider = "commcare_connect"
    (asset,) = generate_connect_assets(
        {
            "visit": {
                "name": "Visit",
                "questions": [
                    {"value": {"en": "/data/bad"}, "type": "Text", "repeat": False},
                    {"value": "/data/name", "type": "Text", "repeat": False},
                    {"value": {"en": "/data/group/x"}, "type": "Text", "repeat": True},
                ],
            }
        },
        tenant,
    )
    assert asset.name == "stg_visits"
    assert 'AS "name"' in asset.sql_content
    assert "bad" not in asset.sql_content
