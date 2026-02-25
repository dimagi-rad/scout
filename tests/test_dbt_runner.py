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
