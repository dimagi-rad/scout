{#
  dbt-postgres names a model's backup/intermediate/temp relations by cutting the model
  name to 63 minus the suffix length (51 bytes for __dbt_backup). Scout model names can
  use all 63 bytes, so sibling models sharing a long prefix (e.g. several
  stg_form_..._repeat_* models) collided on one backup name under threads > 1:
  'relation "..._repeat_joi__dbt_backup" already exists' (SCOUT-DJANGO-32/34).

  Mirrors apps.common.identifiers.fit_identifier: names that fit are unchanged, so
  leftovers from earlier runs are still found and dropped; only names that would be
  truncated get a digest of the full model name woven in before the suffix.
#}

{% macro scout__make_relation_with_suffix(base_relation, suffix, dstring) %}
    {% if dstring %}
      {% set suffix = suffix ~ modules.datetime.datetime.now().strftime("%H%M%S%f") %}
    {% endif %}
    {% set max_length = base_relation.relation_max_name_length() %}
    {% set identifier = base_relation.identifier %}
    {% if (identifier ~ suffix)|length > max_length %}
      {% set tail = "_" ~ local_md5(identifier)[:8] ~ suffix %}
      {% if tail|length > max_length %}
        {% do exceptions.raise_compiler_error("Relation suffix is too long: " ~ suffix) %}
      {% endif %}
      {% set identifier = identifier[:max_length - tail|length].rstrip("_") ~ tail %}
    {% else %}
      {% set identifier = identifier ~ suffix %}
    {% endif %}
    {{ return(base_relation.incorporate(path={"identifier": identifier})) }}
{% endmacro %}

{% macro postgres__make_intermediate_relation(base_relation, suffix) %}
    {{ return(scout__make_relation_with_suffix(base_relation, suffix, dstring=False)) }}
{% endmacro %}

{% macro postgres__make_temp_relation(base_relation, suffix) %}
    {% set temp_relation = scout__make_relation_with_suffix(base_relation, suffix, dstring=True) %}
    {{ return(temp_relation.incorporate(path={"schema": none, "database": none})) }}
{% endmacro %}

{% macro postgres__make_backup_relation(base_relation, backup_relation_type, suffix) %}
    {% set backup_relation = scout__make_relation_with_suffix(base_relation, suffix, dstring=False) %}
    {{ return(backup_relation.incorporate(type=backup_relation_type)) }}
{% endmacro %}
