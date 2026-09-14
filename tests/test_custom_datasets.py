"""Custom dataset SQL must validate syntax, not words inside transcript text."""

import pytest
from sqlglot import exp, parse_one

from apps.semantic.services.custom_datasets import (
    CustomDatasetError,
    compile_custom_dataset_sql,
)


def compile_topics(sql, **tables):
    return compile_custom_dataset_sql(
        sql, allowed_tables={"raw_messages": "raw_messages", **tables}
    )


@pytest.mark.parametrize(
    "text",
    [
        "Account update",
        "Work from home",
        "Join support",
        "from raw_messages",
        "call; don't delete",
        "pg_catalog",
    ],
)
def test_topic_text_is_not_interpreted_as_sql(text):
    escaped = text.replace("'", "''")
    compiled = compile_topics(
        f"SELECT message_id, CASE WHEN content ILIKE '%{escaped}%' "
        f"THEN '{escaped}' ELSE 'Other' END AS topic FROM raw_messages"
    )
    statement = parse_one(compiled, dialect="postgres")

    assert [table.name for table in statement.find_all(exp.Table)] == ["raw_messages"]
    assert {literal.this for literal in statement.find_all(exp.Literal)} == {
        f"%{text}%",
        text,
        "Other",
    }
    assert statement.find(exp.Table).this.quoted is True
    assert statement.args.get("limit") is None


def test_comments_and_multiline_select_are_supported():
    compiled = compile_topics(
        "-- classify update requests from home\nSELECT\nmessage_id, content "
        "FROM raw_messages /* do not delete source data */"
    )
    assert parse_one(compiled, dialect="postgres").find(exp.Table).name == "raw_messages"


def test_cte_columns_and_nested_scopes_resolve_physical_tables():
    compiled = compile_topics(
        "WITH messages(id, topic) AS ("
        "SELECT message_id, metadata->>'topic' FROM raw_messages"
        ") SELECT * FROM messages"
    )
    statement = parse_one(compiled, dialect="postgres")
    tables = {table.name: table for table in statement.find_all(exp.Table)}
    assert tables["raw_messages"].this.quoted
    assert not tables["messages"].this.quoted


def test_cte_names_follow_postgres_identifier_casing():
    compiled = compile_topics("WITH Topics AS (SELECT * FROM RAW_MESSAGES) SELECT * FROM TOPICS")
    tables = list(parse_one(compiled, dialect="postgres").find_all(exp.Table))
    assert sorted(table.name for table in tables) == ["raw_messages", "topics"]


def test_reviewed_topic_labels_can_be_joined_back_to_message_ids():
    compiled = compile_topics(
        "WITH labels(message_id, topic) AS (VALUES ('s1:0', 'Account update')) "
        "SELECT m.message_id, l.topic FROM raw_messages m "
        "JOIN labels l ON m.message_id = l.message_id"
    )
    statement = parse_one(compiled, dialect="postgres")
    assert sorted(table.name for table in statement.find_all(exp.Table)) == [
        "labels",
        "raw_messages",
    ]
    assert {literal.this for literal in statement.find_all(exp.Literal)} == {
        "s1:0",
        "Account update",
    }


def test_topic_tags_can_be_extracted_with_safe_lateral_functions():
    compiled = compile_topics(
        "SELECT m.message_id, tag FROM raw_messages m "
        "CROSS JOIN LATERAL jsonb_array_elements_text(m.tags) AS tag"
    )
    assert 'FROM "raw_messages" AS m' in compiled
    assert "pg_catalog.jsonb_array_elements_text(m.tags)" in compiled


def test_self_named_nonrecursive_cte_still_resolves_its_source():
    compiled = compile_topics(
        "WITH messages AS (SELECT message_id FROM messages) SELECT * FROM messages",
        messages="raw_messages",
    )
    tables = list(parse_one(compiled, dialect="postgres").find_all(exp.Table))
    assert sorted(table.name for table in tables) == ["messages", "raw_messages"]


def test_semantic_table_alias_preserves_qualified_columns():
    compiled = compile_topics("SELECT messages.content FROM messages", messages="raw_messages")
    statement = parse_one(compiled, dialect="postgres")
    table = statement.find(exp.Table)
    assert table.name == "raw_messages"
    assert table.alias == "messages"
    assert statement.find(exp.Column).table == "messages"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM other_messages",
        "SELECT * FROM raw_messages, other_messages",
        "SELECT * FROM raw_messages WHERE EXISTS (SELECT 1 FROM other_messages)",
        "WITH m AS (SELECT * FROM other_messages) SELECT * FROM m",
        "WITH m AS (SELECT * FROM raw_messages) SELECT * FROM m UNION SELECT * FROM other_messages",
        "WITH other_messages AS (SELECT * FROM raw_messages) "
        "SELECT * FROM other_messages UNION SELECT * FROM "
        "(WITH m AS (SELECT * FROM unknown_messages) SELECT * FROM m) nested",
        "SELECT * FROM public.raw_messages",
        "SELECT * FROM another_tenant.raw_messages",
        "SELECT * FROM pg_class",
        "SELECT * FROM information_schema.tables",
        "DELETE FROM raw_messages",
        "WITH d AS (DELETE FROM raw_messages RETURNING *) SELECT * FROM d",
        "SELECT * INTO new_messages FROM raw_messages",
        "SELECT * FROM raw_messages; SELECT 1",
        "SELECT pg_read_file('/etc/passwd') FROM raw_messages",
        "SELECT query_to_xml('SELECT * FROM other_messages', true, true, '')",
        "SELECT custom_classifier(content) FROM raw_messages",
        "SELECT content::custom_type FROM raw_messages",
    ],
)
def test_custom_dataset_rejects_unsafe_or_out_of_scope_sql(sql):
    with pytest.raises(CustomDatasetError):
        compile_topics(sql)


def test_custom_dataset_does_not_cap_the_saved_population():
    compiled = compile_topics("SELECT message_id FROM raw_messages LIMIT 2000")
    statement = parse_one(compiled, dialect="postgres")
    assert statement.args["limit"].expression.this == "2000"


def test_custom_dataset_pins_analytics_functions():
    compiled = compile_topics("SELECT lower(content) AS content FROM raw_messages")
    assert "pg_catalog.LOWER(content)" in compiled
