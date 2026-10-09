from django.db import migrations

# Must match CATALOG_ROLE in cube_config/cube.js. Roles are cluster-wide, so another
# database on the instance may grant to the same role; the reverse migration only
# revokes this database's grant (#421).
CATALOG_ROLE = "scout_cube_catalog"

GRANT = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        CREATE ROLE {role} NOLOGIN;
    END IF;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END
$$;
DO $$
BEGIN
    GRANT {role} TO CURRENT_USER;
EXCEPTION WHEN insufficient_privilege THEN
    -- Another database's user created the role and we lack ADMIN OPTION on it.
    -- Cube checks membership and keeps reading as the owner, so don't fail the deploy.
    RAISE WARNING 'Cannot grant {role} to %; Cube will keep reading its catalog as the owner',
        current_user;
END
$$;
GRANT SELECT ON semantic_cubeschema TO {role};
""".replace("{role}", CATALOG_ROLE)

REVOKE = "REVOKE SELECT ON semantic_cubeschema FROM {role};".replace("{role}", CATALOG_ROLE)


class Migration(migrations.Migration):
    dependencies = [
        ("semantic", "0004_expand_measure_types"),
    ]

    operations = [
        migrations.RunSQL(GRANT, reverse_sql=REVOKE),
    ]
