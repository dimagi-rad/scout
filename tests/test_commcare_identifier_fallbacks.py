"""One non-sluggable upstream identifier must never sink a tenant's whole staging layer.

SCOUT-DJANGO-3D: PR #452 covered form and app display labels. Case types, case
properties, question IDs and repeat-group names still went through the strict
slugifier, and the materializer catches the ValueError per tenant, so a single
non-Latin identifier silently dropped every system asset for that tenant.
"""

import logging
import re

import pytest

from apps.transformations.models import TransformationAsset, TransformationScope
from apps.transformations.services.commcare_staging import (
    generate_system_assets,
    upsert_system_assets,
)
from apps.transformations.services.connect_staging import generate_connect_assets
from apps.users.models import Tenant
from apps.workspaces.models import TenantMetadata

DIGEST_NAME = r"unnamed_[0-9a-f]{8}"
COMMCARE_LOGGER = "apps.transformations.services.commcare_staging"
CONNECT_LOGGER = "apps.transformations.services.connect_staging"


@pytest.fixture
def unsaved_tenant():
    return Tenant(provider="commcare", external_id="synthetic-identifier-fallbacks")


def case_metadata(case_types, properties=()):
    return {
        "case_types": [{"name": name} for name in case_types],
        "app_definitions": [
            {
                "modules": [
                    {"case_type": name, "case_properties": list(properties)} for name in case_types
                ]
            }
        ],
    }


def form_metadata(questions):
    return {"form_definitions": {"urn:synthetic:form": {"name": "Reg", "questions": questions}}}


@pytest.mark.parametrize("case_type", ["日本語", "---", "📝"])
def test_non_slugifiable_case_type_gets_a_digest_model(unsaved_tenant, case_type):
    assets = generate_system_assets(unsaved_tenant, case_metadata([case_type]))
    assert len(assets) == 1
    assert re.fullmatch(rf"stg_case_{DIGEST_NAME}", assets[0].name)
    assert f"WHERE case_type = '{case_type}'" in assets[0].sql_content


def test_digest_case_models_are_distinct_and_stable(unsaved_tenant):
    def names(*case_types):
        return {
            a.sql_content: a.name
            for a in generate_system_assets(unsaved_tenant, case_metadata(case_types))
        }

    first = names("日本語", "한국어")
    assert len(set(first.values())) == 2
    assert first == names("한국어", "日本語")


@pytest.mark.django_db
def test_upsert_accepts_a_non_slugifiable_case_type():
    tenant = Tenant.objects.create(provider="commcare", external_id="synthetic-upsert-fallback")
    tenant_metadata = TenantMetadata(tenant=tenant, metadata=case_metadata(["日本語", "patient"]))
    result = upsert_system_assets(tenant, tenant_metadata)
    assert result["created"] == 2
    names = set(
        TransformationAsset.objects.filter(
            tenant=tenant, scope=TransformationScope.SYSTEM
        ).values_list("name", flat=True)
    )
    assert "stg_case_patient" in names
    assert any(re.fullmatch(rf"stg_case_{DIGEST_NAME}", n) for n in names)


def test_non_slugifiable_case_property_becomes_a_digest_column(unsaved_tenant):
    asset = generate_system_assets(unsaved_tenant, case_metadata(["patient"], ["日本語", "name"]))[
        0
    ]
    assert re.search(rf"properties->>'日本語' AS \"{DIGEST_NAME}\"", asset.sql_content)
    assert "properties->>'name' AS \"name\"" in asset.sql_content


def test_non_slugifiable_question_ids_become_distinct_digest_columns(unsaved_tenant):
    asset = generate_system_assets(
        unsaved_tenant,
        form_metadata(
            [
                {"value": "/data/日本語", "type": "Text"},
                {"value": "/data/한국어", "type": "Text"},
                {"value": "/data/name", "type": "Text"},
            ]
        ),
    )[0]
    assert asset.name == "stg_form_reg"
    columns = re.findall(rf'AS "({DIGEST_NAME})"', asset.sql_content)
    assert len(columns) == 2
    assert len(set(columns)) == 2
    assert 'AS "name"' in asset.sql_content


def test_non_slugifiable_repeat_group_keeps_its_parent_reference(unsaved_tenant):
    parent, repeat = generate_system_assets(
        unsaved_tenant,
        form_metadata([{"value": "/data/日本語/name", "repeat": "/data/日本語", "type": "Text"}]),
    )
    assert parent.name == "stg_form_reg"
    assert re.fullmatch(rf"stg_form_reg__repeat_{DIGEST_NAME}", repeat.name)
    assert "ref('stg_form_reg')" in repeat.sql_content
    assert "ARRAY['data','日本語']::text[]" in repeat.sql_content
    assert repeat.description == "Repeat group '日本語' from stg_form_reg"


def test_one_bad_identifier_does_not_sink_the_tenant(unsaved_tenant):
    metadata = case_metadata(["日本語", "patient"], ["日本語"])
    metadata["form_definitions"] = {
        "urn:synthetic:good": {"name": "Household", "questions": [{"value": "/data/name"}]},
        "urn:synthetic:bad": {
            "name": "Reg",
            "questions": [{"value": "/data/日本語/x", "repeat": "/data/日本語"}],
        },
    }
    names = {a.name for a in generate_system_assets(unsaved_tenant, metadata)}
    assert {"stg_case_patient", "stg_form_household", "stg_form_reg"} <= names
    assert len(names) == 5
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names)


def test_connect_non_slugifiable_question_and_repeat_group(unsaved_tenant):
    unsaved_tenant.provider = "connect"
    assets = generate_connect_assets(
        {
            "visit": {
                "name": "Visit",
                "questions": [
                    {"value": "/data/日本語", "type": "Text", "repeat": False},
                    {"value": "/data/한국어/x", "type": "Text", "repeat": True},
                ],
            }
        },
        unsaved_tenant,
    )
    by_name = {a.name: a for a in assets}
    assert "stg_visits" in by_name
    assert re.search(rf'AS "{DIGEST_NAME}"', by_name["stg_visits"].sql_content)
    repeat = next(n for n in by_name if n != "stg_visits")
    assert re.fullmatch(rf"stg_visits__repeat_{DIGEST_NAME}", repeat)


def info_messages(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


def test_digest_fallbacks_are_logged_once_for_the_tenant(unsaved_tenant, caplog):
    metadata = case_metadata(["日本語", "patient"], ["한국어", "name"])
    metadata["form_definitions"] = {
        "urn:synthetic:good": {"name": "Household", "questions": [{"value": "/data/name"}]},
        "urn:synthetic:bad": {
            "name": {"en": "---"},
            "questions": [{"value": "/data/📝/x", "repeat": "/data/📝"}],
        },
    }
    with caplog.at_level(logging.INFO, logger=COMMCARE_LOGGER):
        generate_system_assets(unsaved_tenant, metadata)

    (message,) = info_messages(caplog)
    assert unsaved_tenant.external_id in message
    assert "case type='日本語'" in message
    assert "case property='한국어'" in message
    assert "form name={'en': '---'}" in message
    assert "repeat group='📝'" in message
    assert "'patient'" not in message
    assert "case property='name'" not in message


def test_nothing_is_logged_when_every_identifier_slugs(unsaved_tenant, caplog):
    with caplog.at_level(logging.INFO, logger=COMMCARE_LOGGER):
        generate_system_assets(unsaved_tenant, case_metadata(["patient"], ["name"]))
    assert info_messages(caplog) == []


def test_connect_digest_fallbacks_are_logged_for_the_tenant(unsaved_tenant, caplog):
    unsaved_tenant.provider = "commcare_connect"
    with caplog.at_level(logging.INFO, logger=CONNECT_LOGGER):
        generate_connect_assets(
            {"visit": {"name": "Visit", "questions": [{"value": "/data/日本語"}]}}, unsaved_tenant
        )
    (message,) = info_messages(caplog)
    assert unsaved_tenant.external_id in message
    assert "question='日本語'" in message


def test_nothing_is_logged_for_a_clean_connect_tenant(unsaved_tenant, caplog):
    unsaved_tenant.provider = "commcare_connect"
    with caplog.at_level(logging.INFO, logger=CONNECT_LOGGER):
        generate_connect_assets(
            {"visit": {"name": "Visit", "questions": [{"value": "/data/name"}]}}, unsaved_tenant
        )
    assert info_messages(caplog) == []
