from unittest.mock import MagicMock, patch


class TestGenerateProfilesYml:
    def test_generates_valid_yaml(self, tmp_path):
        from mcp_server.services.dbt_runner import generate_profiles_yml

        path = tmp_path / "profiles.yml"
        generate_profiles_yml(
            output_path=path,
            schema_name="dimagi",
            db_url="postgresql://svc:pass@localhost:5432/managed_db",
        )

        assert path.exists()
        import yaml

        content = yaml.safe_load(path.read_text())
        profile = content["data_explorer"]["outputs"]["tenant_schema"]
        assert profile["schema"] == "dimagi"
        assert profile["host"] == "localhost"
        assert profile["dbname"] == "managed_db"
        assert profile["type"] == "postgres"

    def test_parses_url_components(self, tmp_path):
        from mcp_server.services.dbt_runner import generate_profiles_yml

        path = tmp_path / "profiles.yml"
        generate_profiles_yml(
            output_path=path,
            schema_name="test_schema",
            db_url="postgresql://myuser:mypassword@db.host.com:5433/analytics",
        )

        import yaml

        content = yaml.safe_load(path.read_text())
        profile = content["data_explorer"]["outputs"]["tenant_schema"]
        assert profile["host"] == "db.host.com"
        assert profile["port"] == 5433
        assert profile["user"] == "myuser"
        assert profile["dbname"] == "analytics"

    def test_percent_encoded_password_is_decoded(self, tmp_path):
        """resolve-database-url.sh URL-encodes the RDS password; the profile must
        decode it so dbt authenticates like psycopg/Django do (SCOUT-DJANGO-1T)."""
        from urllib.parse import quote

        from mcp_server.services.dbt_runner import generate_profiles_yml

        raw_password = "p(a<s>s?[word"
        db_url = f"postgresql://plat%40form:{quote(raw_password, safe='')}@h:5432/db"

        path = tmp_path / "profiles.yml"
        generate_profiles_yml(output_path=path, schema_name="s", db_url=db_url)

        import yaml

        profile = yaml.safe_load(path.read_text())["data_explorer"]["outputs"]["tenant_schema"]
        assert profile["password"] == raw_password
        assert profile["user"] == "plat@form"

    def test_search_path_confined_to_target_schema(self, tmp_path):
        """The profile must pin search_path to the tenant schema only (issue #241,
        04#4): without it dbt creates the relation in the tenant schema but runs
        the SELECT against '$user,public', so every generated staging model that
        does ``FROM raw_cases`` fails silently. The search_path must NOT include
        ``public`` — restricting it to the single schema also blocks unqualified
        cross-tenant reads."""
        from mcp_server.services.dbt_runner import generate_profiles_yml

        path = tmp_path / "profiles.yml"
        generate_profiles_yml(
            output_path=path,
            schema_name="dimagi",
            db_url="postgresql://svc:pass@localhost:5432/managed_db",
        )

        import yaml

        profile = yaml.safe_load(path.read_text())["data_explorer"]["outputs"]["tenant_schema"]
        assert profile["search_path"] == "dimagi"

    def test_dbt_role_confines_to_low_privilege_role(self, tmp_path):
        """The profile must assume a dedicated low-privilege role via ``role`` so
        dbt does NOT run user-authored SQL as the full managed-DB superuser
        (issue #241, 04#3 SECURITY). When a confinement role is passed, dbt issues
        SET ROLE to it on every new connection."""
        from mcp_server.services.dbt_runner import generate_profiles_yml

        path = tmp_path / "profiles.yml"
        generate_profiles_yml(
            output_path=path,
            schema_name="dimagi",
            db_url="postgresql://svc:pass@localhost:5432/managed_db",
            confinement_role="dimagi_dbt",
        )

        import yaml

        profile = yaml.safe_load(path.read_text())["data_explorer"]["outputs"]["tenant_schema"]
        assert profile["role"] == "dimagi_dbt"

    def test_no_role_key_when_confinement_role_absent(self, tmp_path):
        """Backwards-compatible: when no confinement role is supplied the profile
        omits ``role`` entirely (dbt connects as the configured user)."""
        from mcp_server.services.dbt_runner import generate_profiles_yml

        path = tmp_path / "profiles.yml"
        generate_profiles_yml(
            output_path=path,
            schema_name="dimagi",
            db_url="postgresql://svc:pass@localhost:5432/managed_db",
        )

        import yaml

        profile = yaml.safe_load(path.read_text())["data_explorer"]["outputs"]["tenant_schema"]
        assert "role" not in profile


class TestRunDbt:
    def test_returns_success_result(self, tmp_path):
        from mcp_server.services.dbt_runner import run_dbt

        node_cases = MagicMock()
        node_cases.name = "stg_cases"
        node_forms = MagicMock()
        node_forms.name = "stg_forms"
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.result = [
            MagicMock(node=node_cases, status="success"),
            MagicMock(node=node_forms, status="success"),
        ]

        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            result = run_dbt(
                dbt_project_dir=str(tmp_path),
                profiles_dir=str(tmp_path),
                models=["stg_cases", "stg_forms"],
            )

        assert result["success"] is True
        assert result["models"]["stg_cases"] == "success"
        assert result["models"]["stg_forms"] == "success"

    def test_returns_failure_when_dbt_fails(self, tmp_path):
        from mcp_server.services.dbt_runner import run_dbt

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.result = []
        mock_result.exception = RuntimeError("dbt compilation error")

        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            result = run_dbt(
                dbt_project_dir=str(tmp_path),
                profiles_dir=str(tmp_path),
                models=["stg_cases"],
            )

        assert result["success"] is False
        assert "error" in result

    def test_surfaces_node_error_when_no_exception(self, tmp_path):
        """Node-level model failures (success=False, exception=None) must surface
        the per-node message, not the opaque 'dbt run failed' (SCOUT-DJANGO-1T)."""
        from mcp_server.services.dbt_runner import run_dbt

        node = MagicMock()
        node.name = "stg_visits"
        failed = MagicMock(node=node, status="error")
        failed.message = 'column "user_id" does not exist'

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.exception = None
        mock_result.result = [failed]

        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            result = run_dbt(
                dbt_project_dir=str(tmp_path),
                profiles_dir=str(tmp_path),
                models=["stg_visits"],
            )

        assert result["success"] is False
        assert "stg_visits" in result["error"]
        assert 'column "user_id" does not exist' in result["error"]

    def test_passes_correct_cli_args(self, tmp_path):
        from mcp_server.services.dbt_runner import run_dbt

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.result = []

        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            run_dbt(
                dbt_project_dir="/path/to/project",
                profiles_dir="/path/to/profiles",
                models=["stg_cases", "stg_forms"],
            )

        call_args = mock_runner.invoke.call_args[0][0]
        assert "run" in call_args
        assert "--project-dir" in call_args
        assert "/path/to/project" in call_args
        assert "--profiles-dir" in call_args
        assert "/path/to/profiles" in call_args
        assert "stg_cases" in " ".join(call_args)

    def test_lock_is_acquired(self, tmp_path):
        """Verify the threading lock is acquired during a dbt run."""
        from mcp_server.services.dbt_runner import _dbt_lock, run_dbt

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.result = []
        mock_runner = MagicMock()
        mock_runner.invoke.return_value = mock_result

        lock_was_held = []

        def invoke_side_effect(*args, **kwargs):
            lock_was_held.append(_dbt_lock.locked())
            return mock_result

        mock_runner.invoke.side_effect = invoke_side_effect

        with patch("mcp_server.services.dbt_runner.dbtRunner", return_value=mock_runner):
            run_dbt(str(tmp_path), str(tmp_path), ["stg_cases"])

        assert lock_was_held == [True]
