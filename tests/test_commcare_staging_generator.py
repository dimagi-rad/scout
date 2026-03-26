"""Tests for CommCare staging SQL generator (Milestone 4)."""

from __future__ import annotations

import pytest

from apps.transformations.models import TransformationScope
from apps.transformations.services.commcare_staging import (
    generate_system_assets,
    slugify_model_name,
    upsert_system_assets,
)


@pytest.fixture
def tenant(db):
    from apps.users.models import Tenant

    return Tenant.objects.create(external_id="test-domain", provider="commcare")


# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_metadata(
    *,
    case_types=None,
    form_definitions=None,
    app_definitions=None,
):
    """Build a metadata dict matching CommCareMetadataLoader output."""
    return {
        "app_definitions": app_definitions or [],
        "case_types": case_types or [],
        "form_definitions": form_definitions or {},
    }


def _make_full_metadata():
    """Metadata with 2 case types, 3 forms (one with a repeat group)."""
    return _make_metadata(
        app_definitions=[
            {
                "id": "app_abc",
                "name": "CHW App",
                "modules": [
                    {
                        "name": "Patient Registration",
                        "case_type": "patient",
                        "case_properties": [
                            {"key": "dob"},
                            {"key": "gender"},
                            {"key": "village"},
                        ],
                        "forms": [
                            {
                                "xmlns": "http://openrosa.org/formdesigner/reg1",
                                "name": "Patient Registration",
                                "questions": [
                                    {
                                        "label": "Patient Name",
                                        "tag": "input",
                                        "value": "/data/patient_name",
                                    },
                                    {
                                        "label": "Age",
                                        "tag": "input",
                                        "value": "/data/age",
                                        "type": "Int",
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "name": "Household Visit",
                        "case_type": "household",
                        "case_properties": [
                            {"key": "address"},
                            {"key": "num_members"},
                        ],
                        "forms": [
                            {
                                "xmlns": "http://openrosa.org/formdesigner/visit1",
                                "name": "Household Visit",
                                "questions": [
                                    {
                                        "label": "Visit Date",
                                        "tag": "input",
                                        "value": "/data/visit_date",
                                        "type": "Date",
                                    },
                                    {
                                        "label": "Notes",
                                        "tag": "input",
                                        "value": "/data/notes",
                                        "type": "Text",
                                    },
                                ],
                            },
                            {
                                "xmlns": "http://openrosa.org/formdesigner/follow1",
                                "name": "Follow-up Visit",
                                "questions": [
                                    {
                                        "label": "Status",
                                        "tag": "select1",
                                        "value": "/data/status",
                                        "type": "Select",
                                    },
                                    {
                                        "label": "Child Name",
                                        "tag": "input",
                                        "value": "/data/children/child_name",
                                        "type": "Text",
                                        "repeat": "/data/children",
                                    },
                                    {
                                        "label": "Child Age",
                                        "tag": "input",
                                        "value": "/data/children/child_age",
                                        "type": "Int",
                                        "repeat": "/data/children",
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        ],
        case_types=[
            {
                "name": "patient",
                "app_id": "app_abc",
                "app_name": "CHW App",
                "module_name": "Patient Registration",
            },
            {
                "name": "household",
                "app_id": "app_abc",
                "app_name": "CHW App",
                "module_name": "Household Visit",
            },
        ],
        form_definitions={
            "http://openrosa.org/formdesigner/reg1": {
                "name": "Patient Registration",
                "app_name": "CHW App",
                "module_name": "Patient Registration",
                "case_type": "patient",
                "questions": [
                    {
                        "label": "Patient Name",
                        "tag": "input",
                        "value": "/data/patient_name",
                    },
                    {
                        "label": "Age",
                        "tag": "input",
                        "value": "/data/age",
                        "type": "Int",
                    },
                ],
            },
            "http://openrosa.org/formdesigner/visit1": {
                "name": "Household Visit",
                "app_name": "CHW App",
                "module_name": "Household Visit",
                "case_type": "household",
                "questions": [
                    {
                        "label": "Visit Date",
                        "tag": "input",
                        "value": "/data/visit_date",
                        "type": "Date",
                    },
                    {
                        "label": "Notes",
                        "tag": "input",
                        "value": "/data/notes",
                        "type": "Text",
                    },
                ],
            },
            "http://openrosa.org/formdesigner/follow1": {
                "name": "Follow-up Visit",
                "app_name": "CHW App",
                "module_name": "Household Visit",
                "case_type": "household",
                "questions": [
                    {
                        "label": "Status",
                        "tag": "select1",
                        "value": "/data/status",
                        "type": "Select",
                    },
                    {
                        "label": "Child Name",
                        "tag": "input",
                        "value": "/data/children/child_name",
                        "type": "Text",
                        "repeat": "/data/children",
                    },
                    {
                        "label": "Child Age",
                        "tag": "input",
                        "value": "/data/children/child_age",
                        "type": "Int",
                        "repeat": "/data/children",
                    },
                ],
            },
        },
    )


# ── Slugification tests ────────────────────────────────────────────────────


class TestSlugifyModelName:
    def test_spaces_to_underscores(self):
        assert slugify_model_name("Follow-up Visit") == "follow_up_visit"

    def test_special_chars_stripped(self):
        assert slugify_model_name("Visit #2") == "visit_2"

    def test_dots_replaced(self):
        assert slugify_model_name("v1.2.form") == "v1_2_form"

    def test_consecutive_underscores_collapsed(self):
        assert slugify_model_name("a---b___c") == "a_b_c"

    def test_leading_trailing_stripped(self):
        assert slugify_model_name("__hello__") == "hello"

    def test_lowercase(self):
        assert slugify_model_name("MyForm") == "myform"

    def test_empty_result_raises(self):
        with pytest.raises(ValueError, match="Cannot generate a valid model name"):
            slugify_model_name("---")

    def test_unicode_only_raises(self):
        with pytest.raises(ValueError, match="Cannot generate a valid model name"):
            slugify_model_name("日本語")


# ── Case type generation ───────────────────────────────────────────────────


@pytest.mark.django_db
class TestCaseTypeGeneration:
    def test_generates_assets_for_each_case_type(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        case_assets = [a for a in assets if a.name.startswith("stg_case_")]
        assert len(case_assets) == 2
        names = {a.name for a in case_assets}
        assert "stg_case_patient" in names
        assert "stg_case_household" in names

    def test_case_sql_extracts_properties(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        patient_asset = next(a for a in assets if a.name == "stg_case_patient")
        sql = patient_asset.sql_content
        assert """properties->>'dob' AS "dob\"""" in sql
        assert """properties->>'gender' AS "gender\"""" in sql
        assert """properties->>'village' AS "village\"""" in sql

    def test_case_sql_has_core_columns(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        patient_asset = next(a for a in assets if a.name == "stg_case_patient")
        sql = patient_asset.sql_content
        assert "case_id" in sql
        assert "case_type" in sql
        assert "case_name" in sql
        assert "owner_id" in sql
        assert 'date_opened::timestamp AS "date_opened"' in sql
        assert 'last_modified::timestamp AS "last_modified"' in sql

    def test_case_sql_filters_by_case_type(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        patient_asset = next(a for a in assets if a.name == "stg_case_patient")
        assert "WHERE case_type = 'patient'" in patient_asset.sql_content

    def test_case_sql_references_raw_cases_directly(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        patient_asset = next(a for a in assets if a.name == "stg_case_patient")
        assert "FROM raw_cases" in patient_asset.sql_content
        assert "ref(" not in patient_asset.sql_content


# ── Form generation ─────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestFormGeneration:
    def test_generates_assets_for_each_form(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        form_assets = [
            a for a in assets if a.name.startswith("stg_form_") and "__repeat_" not in a.name
        ]
        assert len(form_assets) == 3

    def test_form_sql_extracts_questions(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        reg_asset = next(a for a in assets if a.name == "stg_form_patient_registration")
        sql = reg_asset.sql_content
        assert "form_data #>> ARRAY['data','patient_name']::text[]" in sql
        assert 'AS "patient_name"' in sql

    def test_form_sql_applies_type_cast(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        reg_asset = next(a for a in assets if a.name == "stg_form_patient_registration")
        sql = reg_asset.sql_content
        assert "NULLIF(form_data #>> ARRAY['data','age']::text[], '')::integer AS \"age\"" in sql

    def test_form_sql_date_cast(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        visit_asset = next(a for a in assets if a.name == "stg_form_household_visit")
        sql = visit_asset.sql_content
        assert (
            "NULLIF(form_data #>> ARRAY['data','visit_date']::text[], '')::date"
            ' AS "visit_date"' in sql
        )

    def test_form_sql_no_cast_for_text(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        visit_asset = next(a for a in assets if a.name == "stg_form_household_visit")
        sql = visit_asset.sql_content
        assert """form_data #>> ARRAY['data','notes']::text[] AS "notes\"""" in sql
        assert "NULLIF" not in sql.split('"notes"')[0].split("\n")[-1]

    def test_form_sql_passes_through_form_data(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        reg_asset = next(a for a in assets if a.name == "stg_form_patient_registration")
        sql = reg_asset.sql_content
        # form_data must be in SELECT so repeat group children can reference it via ref()
        assert "form_data" in sql.split("FROM")[0]

    def test_form_sql_references_raw_forms_directly(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        reg_asset = next(a for a in assets if a.name == "stg_form_patient_registration")
        assert "FROM raw_forms" in reg_asset.sql_content
        assert "ref(" not in reg_asset.sql_content

    def test_form_sql_skips_repeat_questions(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        follow_asset = next(a for a in assets if a.name == "stg_form_follow_up_visit")
        sql = follow_asset.sql_content
        assert "child_name" not in sql
        assert "child_age" not in sql
        assert 'AS "status"' in sql


# ── Repeat group generation ─────────────────────────────────────────────────


@pytest.mark.django_db
class TestRepeatGroupGeneration:
    def test_generates_repeat_group_asset(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        repeat_assets = [a for a in assets if "__repeat_" in a.name]
        assert len(repeat_assets) == 1
        assert repeat_assets[0].name == "stg_form_follow_up_visit__repeat_children"

    def test_repeat_sql_uses_jsonb_array_elements(self, tenant):
        repeat = next(
            a
            for a in generate_system_assets(tenant, _make_full_metadata())
            if "__repeat_" in a.name
        )
        assert "jsonb_array_elements" in repeat.sql_content
        assert "WITH ORDINALITY" in repeat.sql_content

    def test_repeat_sql_references_parent_via_ref(self, tenant):
        repeat = next(
            a
            for a in generate_system_assets(tenant, _make_full_metadata())
            if "__repeat_" in a.name
        )
        assert "{{ ref('stg_form_follow_up_visit') }}" in repeat.sql_content

    def test_repeat_sql_has_repeat_index(self, tenant):
        repeat = next(
            a
            for a in generate_system_assets(tenant, _make_full_metadata())
            if "__repeat_" in a.name
        )
        assert '"repeat_index"' in repeat.sql_content

    def test_repeat_sql_extracts_child_questions(self, tenant):
        repeat = next(
            a
            for a in generate_system_assets(tenant, _make_full_metadata())
            if "__repeat_" in a.name
        )
        sql = repeat.sql_content
        assert 'AS "child_name"' in sql
        assert 'AS "child_age"' in sql

    def test_repeat_sql_applies_type_cast_to_children(self, tenant):
        repeat = next(
            a
            for a in generate_system_assets(tenant, _make_full_metadata())
            if "__repeat_" in a.name
        )
        assert '::integer AS "child_age"' in repeat.sql_content

    def test_repeat_sql_uses_original_field_name_for_json_key(self, tenant):
        """JSON key should use the original leaf name, not the slugified alias."""
        metadata = _make_metadata(
            form_definitions={
                "xmlns_repeat": {
                    "name": "Mixed Case Form",
                    "app_name": "App",
                    "questions": [
                        {
                            "label": "First Name",
                            "value": "/data/items/First_Name",
                            "type": "Text",
                            "repeat": "/data/items",
                        },
                    ],
                },
            },
        )
        assets = generate_system_assets(tenant, metadata)
        repeat = next(a for a in assets if "__repeat_" in a.name)
        sql = repeat.sql_content
        # Should use original "First_Name", not slugified "first_name"
        assert "elem.value->>'First_Name'" in sql
        assert 'AS "first_name"' in sql


# ── Disambiguation ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestDisambiguation:
    def test_duplicate_form_names_get_app_suffix(self, tenant):
        metadata = _make_metadata(
            app_definitions=[
                {"id": "app1", "name": "App One", "modules": []},
                {"id": "app2", "name": "App Two", "modules": []},
            ],
            case_types=[],
            form_definitions={
                "xmlns_a": {
                    "name": "Registration",
                    "app_name": "App One",
                    "questions": [],
                },
                "xmlns_b": {
                    "name": "Registration",
                    "app_name": "App Two",
                    "questions": [],
                },
            },
        )
        assets = generate_system_assets(tenant, metadata)
        names = {a.name for a in assets}
        assert "stg_form_registration" in names
        assert "stg_form_registration_app_two_1" in names


# ── Column alias deduplication ──────────────────────────────────────────────


@pytest.mark.django_db
class TestColumnAliasDeduplication:
    def test_duplicate_property_slugs_get_numeric_suffix(self, tenant):
        """Properties that slugify identically get _2, _3 suffixes."""
        metadata = _make_metadata(
            app_definitions=[
                {
                    "id": "a",
                    "name": "App",
                    "modules": [
                        {
                            "case_type": "test",
                            "case_properties": [
                                {"key": "foo-bar"},
                                {"key": "foo.bar"},
                            ],
                        }
                    ],
                }
            ],
            case_types=[{"name": "test", "app_id": "a", "app_name": "App"}],
        )
        assets = generate_system_assets(tenant, metadata)
        case_asset = next(a for a in assets if a.name == "stg_case_test")
        sql = case_asset.sql_content
        assert '"foo_bar"' in sql
        assert '"foo_bar_2"' in sql

    def test_property_colliding_with_core_column_gets_suffix(self, tenant):
        """A property named 'closed' (same as core column) gets _2 suffix."""
        metadata = _make_metadata(
            app_definitions=[
                {
                    "id": "a",
                    "name": "App",
                    "modules": [
                        {
                            "case_type": "test",
                            "case_properties": [{"key": "closed"}],
                        }
                    ],
                }
            ],
            case_types=[{"name": "test", "app_id": "a", "app_name": "App"}],
        )
        assets = generate_system_assets(tenant, metadata)
        case_asset = next(a for a in assets if a.name == "stg_case_test")
        sql = case_asset.sql_content
        # Core "closed" column is bare, property gets suffixed alias
        assert '"closed_2"' in sql

    def test_form_question_colliding_with_fixed_column_gets_suffix(self, tenant):
        """A question whose leaf slugifies to 'form_id' gets _2 suffix."""
        metadata = _make_metadata(
            form_definitions={
                "xmlns_x": {
                    "name": "Collision Form",
                    "app_name": "App",
                    "questions": [
                        {"label": "Form ID", "value": "/data/form_id", "type": "Text"},
                    ],
                },
            },
        )
        assets = generate_system_assets(tenant, metadata)
        form_asset = next(a for a in assets if a.name.startswith("stg_form_"))
        sql = form_asset.sql_content
        assert '"form_id_2"' in sql

    def test_repeat_question_colliding_with_fixed_column_gets_suffix(self, tenant):
        """A repeat child question whose leaf slugifies to 'form_id' gets _2 suffix."""
        metadata = _make_metadata(
            form_definitions={
                "xmlns_r": {
                    "name": "Repeat Collision",
                    "app_name": "App",
                    "questions": [
                        {
                            "label": "Form ID",
                            "value": "/data/items/form_id",
                            "type": "Text",
                            "repeat": "/data/items",
                        },
                    ],
                },
            },
        )
        assets = generate_system_assets(tenant, metadata)
        repeat = next(a for a in assets if "__repeat_" in a.name)
        sql = repeat.sql_content
        assert '"form_id_2"' in sql

    def test_three_plus_form_name_collisions_stay_unique(self, tenant):
        """3+ forms with the same name from the same app get unique slugs."""
        metadata = _make_metadata(
            form_definitions={
                "xmlns_a": {"name": "Follow-up", "app_name": "App", "questions": []},
                "xmlns_b": {"name": "Follow-up", "app_name": "App", "questions": []},
                "xmlns_c": {"name": "Follow-up", "app_name": "App", "questions": []},
            },
        )
        assets = generate_system_assets(tenant, metadata)
        names = [a.name for a in assets]
        assert len(names) == len(set(names)), f"Duplicate model names: {names}"


# ── SQL escaping ────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestSqlEscaping:
    def test_case_type_with_single_quote_is_escaped(self, tenant):
        metadata = _make_metadata(
            case_types=[{"name": "O'Brien", "app_id": "a", "app_name": "App"}],
        )
        assets = generate_system_assets(tenant, metadata)
        case_asset = next(a for a in assets if a.name.startswith("stg_case_"))
        assert "O''Brien" in case_asset.sql_content
        assert "O'Brien'" not in case_asset.sql_content

    def test_form_xmlns_with_single_quote_is_escaped(self, tenant):
        metadata = _make_metadata(
            form_definitions={
                "http://example.com/f'rm": {
                    "name": "Test Form",
                    "app_name": "App",
                    "questions": [],
                },
            },
        )
        assets = generate_system_assets(tenant, metadata)
        form_asset = next(a for a in assets if a.name.startswith("stg_form_"))
        assert "f''rm" in form_asset.sql_content

    def test_json_path_with_comma_uses_array_syntax(self, tenant):
        """Commas in path segments are safely handled by ARRAY[] syntax."""
        metadata = _make_metadata(
            form_definitions={
                "xmlns_comma": {
                    "name": "Comma Form",
                    "app_name": "App",
                    "questions": [
                        {"label": "Field", "value": "/data/foo,bar", "type": "Text"},
                    ],
                },
            },
        )
        assets = generate_system_assets(tenant, metadata)
        form_asset = next(a for a in assets if a.name.startswith("stg_form_"))
        sql = form_asset.sql_content
        # ARRAY syntax keeps 'foo,bar' as a single element
        assert "ARRAY['data','foo,bar']::text[]" in sql

    def test_case_property_with_single_quote_is_escaped(self, tenant):
        metadata = _make_metadata(
            app_definitions=[
                {
                    "id": "a",
                    "name": "App",
                    "modules": [
                        {
                            "case_type": "test",
                            "case_properties": [{"key": "mother's_name"}],
                        }
                    ],
                }
            ],
            case_types=[{"name": "test", "app_id": "a", "app_name": "App"}],
        )
        assets = generate_system_assets(tenant, metadata)
        case_asset = next(a for a in assets if a.name == "stg_case_test")
        assert "mother''s_name'" in case_asset.sql_content

    def test_reserved_word_column_alias_is_quoted(self, tenant):
        """PostgreSQL reserved words are safe because all aliases are double-quoted."""
        metadata = _make_metadata(
            app_definitions=[
                {
                    "id": "a",
                    "name": "App",
                    "modules": [
                        {
                            "case_type": "test",
                            "case_properties": [{"key": "order"}, {"key": "group"}],
                        }
                    ],
                }
            ],
            case_types=[{"name": "test", "app_id": "a", "app_name": "App"}],
        )
        assets = generate_system_assets(tenant, metadata)
        case_asset = next(a for a in assets if a.name == "stg_case_test")
        sql = case_asset.sql_content
        assert 'AS "order"' in sql
        assert 'AS "group"' in sql


# ── Asset properties ────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAssetProperties:
    def test_all_assets_have_system_scope(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        assert all(a.scope == TransformationScope.SYSTEM for a in assets)

    def test_all_assets_have_no_created_by(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        assert all(a.created_by is None for a in assets)

    def test_all_assets_assigned_to_tenant(self, tenant):
        assets = generate_system_assets(tenant, _make_full_metadata())
        assert all(a.tenant is tenant for a in assets)


# ── Empty metadata ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestEmptyMetadata:
    def test_empty_metadata_returns_empty_list(self, tenant):
        assets = generate_system_assets(tenant, _make_metadata())
        assert assets == []


# ── Upsert function ─────────────────────────────────────────────────────────


@pytest.fixture
def tenant_metadata(tenant):
    from apps.users.models import TenantMembership, User
    from apps.workspaces.models import TenantMetadata

    user = User.objects.create_user(email="test@example.com", password="testpass")
    membership = TenantMembership.objects.create(user=user, tenant=tenant)
    return TenantMetadata.objects.create(
        tenant_membership=membership,
        metadata=_make_full_metadata(),
    )


@pytest.mark.django_db
class TestUpsertSystemAssets:
    def test_first_run_all_created(self, tenant, tenant_metadata):
        result = upsert_system_assets(tenant, tenant_metadata)
        assert result["created"] > 0
        assert result["updated"] == 0
        assert result["total"] == result["created"]

    def test_second_run_all_updated(self, tenant, tenant_metadata):
        upsert_system_assets(tenant, tenant_metadata)
        result = upsert_system_assets(tenant, tenant_metadata)
        assert result["created"] == 0
        assert result["updated"] > 0
        assert result["total"] == result["updated"]

    def test_additional_case_type_creates_new(self, tenant, tenant_metadata):
        first = upsert_system_assets(tenant, tenant_metadata)

        tenant_metadata.metadata["case_types"].append(
            {"name": "visit", "app_id": "app_abc", "app_name": "CHW App"}
        )
        tenant_metadata.save()

        second = upsert_system_assets(tenant, tenant_metadata)
        assert second["created"] == 1
        assert second["updated"] == first["total"]
        assert second["total"] == first["total"] + 1

    def test_empty_metadata_returns_zeros(self, tenant):
        from apps.users.models import TenantMembership, User
        from apps.workspaces.models import TenantMetadata

        user = User.objects.create_user(email="empty@example.com", password="testpass")
        membership = TenantMembership.objects.create(user=user, tenant=tenant)
        empty_meta = TenantMetadata.objects.create(
            tenant_membership=membership, metadata=_make_metadata()
        )
        result = upsert_system_assets(tenant, empty_meta)
        assert result == {"created": 0, "updated": 0, "total": 0}
