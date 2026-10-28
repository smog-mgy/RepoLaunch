import hashlib
import json
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
from tools import integrations as infra


def test_compose_is_checked_in_and_matches_generator():
    path = Path(infra.__file__).resolve().parents[1] / "infra" / "compose.yaml"
    assert json.loads(path.read_text(encoding="utf-8")) == infra.build_compose()


def test_all_published_ports_are_localhost_only():
    for service in infra.build_compose()["services"].values():
        for binding in service.get("ports", []):
            assert binding.startswith("127.0.0.1:")
        assert not service.get("privileged")
        assert "/var/run/docker.sock" not in json.dumps(service)


def test_persistence_stays_below_project_data_directory():
    compose = infra.build_compose()
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            if name == "clickhouse":
                assert volume["type"] == "volume"
                assert volume["source"] in {"clickhouse-data", "clickhouse-logs"}
                assert compose["volumes"][volume["source"]]["labels"]["repolaunch.stack"] == "integrations"
            else:
                assert volume["type"] == "bind"
                assert volume["source"].startswith("../.repolaunch/services/")
                assert ".." not in Path(volume["source"]).parts[1:]
    assert set(compose["volumes"]) == {"clickhouse-data", "clickhouse-logs"}


def test_images_have_exact_version_tags():
    assert infra.PROJECT == "repolaunch-integrations"
    for image in infra.IMAGES.values():
        tag = image.rsplit(":", 1)[1]
        assert tag not in {"latest", "3", "7", "8.4", "16"}
    assert infra.IMAGES["mysql"].startswith("mysql:8.4.")
    assert infra.IMAGES["milvus"].startswith("milvusdb/milvus:v2.6.")


def test_langfuse_headless_setup_and_shared_storage_are_explicit():
    services = infra.build_compose()["services"]
    env = services["langfuse-web"]["environment"]
    assert services["langfuse-worker"]["environment"]["HOSTNAME"] == "0.0.0.0"
    assert env["HOSTNAME"] == "0.0.0.0"
    assert env["PORT"] == "3000"
    assert env["LANGFUSE_INIT_ORG_ID"] == "repolaunch-local"
    assert env["LANGFUSE_INIT_PROJECT_ID"] == "repolaunch"
    assert env["LANGFUSE_INIT_USER_EMAIL"] == "admin@repolaunch.local"
    assert env["LANGFUSE_S3_EVENT_UPLOAD_BUCKET"] == "langfuse"
    assert env["LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT"] == "http://minio:9000"
    assert services["milvus"]["environment"]["MINIO_ADDRESS"] == "minio:9000"
    assert env["TELEMETRY_ENABLED"] == "false"


def test_prepare_generates_credentials_once_without_app_config(tmp_path):
    first = infra.prepare(tmp_path)
    secret_file = tmp_path / ".repolaunch" / "services" / "services.env"
    initial_bytes = secret_file.read_bytes()
    second = infra.prepare(tmp_path)
    assert first == second
    assert secret_file.read_bytes() == initial_bytes
    assert len(first["LANGFUSE_ENCRYPTION_KEY"]) == 64
    assert first["LANGFUSE_PUBLIC_KEY"].startswith("pk-lf-")
    assert not (tmp_path / ".env").exists()


def test_prepare_never_rekeys_existing_data(tmp_path):
    folder = tmp_path / ".repolaunch" / "services" / "mysql"
    folder.mkdir(parents=True)
    (folder / "existing-data").write_bytes(b"database")
    with pytest.raises(ValueError, match="已有服务数据"):
        infra.prepare(tmp_path)
    assert not (folder.parent / "services.env").exists()


def test_prepare_refuses_incomplete_credential_file(tmp_path):
    folder = tmp_path / ".repolaunch" / "services"
    folder.mkdir(parents=True)
    (folder / "services.env").write_text("MYSQL_PASSWORD=original\n")
    with pytest.raises(ValueError, match="配置不完整"):
        infra.prepare(tmp_path)
    assert (folder / "services.env").read_text() == "MYSQL_PASSWORD=original\n"


def test_generated_passwords_do_not_enter_tracked_compose(tmp_path):
    config = infra.prepare(tmp_path)
    serialized = json.dumps(infra.build_compose())
    for key in infra.SECRET_KEYS:
        assert config[key] not in serialized


def test_app_env_update_preserves_model_key_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# local model\nREPOLAUNCH_API_KEY=user-secret\nDATABASE_URL=old\nUNRELATED=keep\nDATABASE_URL=duplicate\n", encoding="utf-8")
    infra.update_app_env(tmp_path, {"DATABASE_URL": "new", "LANGFUSE_BASE_URL": "http://127.0.0.1:30300"})
    result = path.read_text(encoding="utf-8")
    assert "REPOLAUNCH_API_KEY=user-secret" in result
    assert "# local model" in result
    assert "UNRELATED=keep" in result
    assert result.count("DATABASE_URL=") == 1
    assert "DATABASE_URL=new" in result


def test_app_configuration_never_contains_model_or_embedding_credentials(tmp_path):
    config = infra.prepare(tmp_path)
    app = infra.app_configuration(config)
    assert app["DATABASE_URL"].startswith("mysql+pymysql://repolaunch:")
    assert app["REPOLAUNCH_MILVUS_URI"].startswith("http://127.0.0.1:")
    assert "REPOLAUNCH_API_KEY" not in app
    assert "REPOLAUNCH_EMBEDDING_API_KEY" not in app


def test_error_output_redacts_all_generated_passwords(tmp_path):
    config = infra.prepare(tmp_path)
    message = " ".join(config[key] for key in infra.SECRET_KEYS)
    result = infra.sanitized(message, config)
    for key in infra.SECRET_KEYS:
        assert config[key] not in result


def test_busy_unowned_ports_fail_without_termination(tmp_path, monkeypatch):
    config = infra.prepare(tmp_path)
    listener = Mock()
    listener.__enter__ = Mock(return_value=listener)
    listener.__exit__ = Mock(return_value=False)
    listener.bind.side_effect = OSError("occupied")
    monkeypatch.setattr(infra.socket, "socket", Mock(return_value=listener))
    with pytest.raises(ValueError, match="未停止任何程序"):
        infra.ensure_ports_free(config)


def test_existing_project_ports_are_allowed_on_retry(tmp_path, monkeypatch):
    config = infra.prepare(tmp_path)
    create = Mock(side_effect=AssertionError("must not bind an owned port"))
    monkeypatch.setattr(infra.socket, "socket", create)
    infra.ensure_ports_free(config, {int(config[k]) for k in infra.PORTS})
    create.assert_not_called()


def test_failed_verification_cannot_update_app_environment(tmp_path, monkeypatch):
    import repolaunch.engine
    monkeypatch.setattr(repolaunch.engine, "docker_status", Mock(return_value={"available": True}))
    monkeypatch.setattr(infra, "compose_command", Mock(return_value=""))
    monkeypatch.setattr(infra, "docker_command", Mock(return_value=""))
    monkeypatch.setattr(infra, "ensure_ports_free", Mock())
    monkeypatch.setattr(infra, "verify_services", Mock(side_effect=RuntimeError("unhealthy")))
    update = Mock()
    monkeypatch.setattr(infra, "update_app_env", update)
    clock = iter([0, 100])
    monkeypatch.setattr(infra.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="未修改应用配置"):
        infra._deploy_unlocked(tmp_path)
    update.assert_not_called()


def task(identity):
    return {"id": identity, "status": "succeeded", "created_at": "2026-10-26T00:00:00Z", "updated_at": "2026-10-26T00:00:00Z", "events": [], "plan": {"entrypoint": "app:app"}}


def test_history_migration_preserves_source_and_records(tmp_path):
    from repolaunch.storage import TaskStore
    folder = tmp_path / ".repolaunch"
    source = TaskStore(folder)
    source.create(task("historical"))
    source.close()
    database = folder / "tasks.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    target_url = "sqlite:///" + (tmp_path / "target.sqlite3").as_posix()
    result = infra.migrate_history(tmp_path, target_url)
    assert result["copied"] == 1
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    target = TaskStore(folder, target_url)
    try:
        assert target.get("historical")["plan"]["entrypoint"] == "app:app"
    finally:
        target.close()


def test_history_migration_never_overwrites_nonempty_target(tmp_path):
    from repolaunch.storage import TaskStore
    folder = tmp_path / ".repolaunch"
    source = TaskStore(folder)
    source.create(task("historical"))
    source.close()
    target_url = "sqlite:///" + (tmp_path / "target.sqlite3").as_posix()
    target = TaskStore(folder, target_url)
    target.create(task("existing"))
    target.close()
    result = infra.migrate_history(tmp_path, target_url)
    assert result["reason"] == "target_not_empty"
    target = TaskStore(folder, target_url)
    try:
        assert len(target.list()) == 1
        assert target.get("existing")["id"] == "existing"
    finally:
        target.close()


def test_metadata_exposes_endpoints_not_passwords(tmp_path):
    config = infra.prepare(tmp_path)
    metadata = infra.deployment_metadata(config, {"mysql": {"ready": True}}, {"copied": 0})
    serialized = json.dumps(metadata)
    for key in infra.SECRET_KEYS:
        assert config[key] not in serialized
    assert metadata["project"] == infra.PROJECT


def test_minio_uses_verified_vendor_archive_and_portable_health_probe():
    minio = infra.build_compose()["services"]["minio"]
    assert minio["image"] == "bitnamilegacy/minio:2025.7.23-debian-12-r5"
    assert "/opt/bitnami/minio/bin/minio server" in minio["command"][0]
    probe = minio["healthcheck"]["test"]
    assert probe[:3] == ["CMD", "/bin/bash", "-ec"]
    assert "/minio/health/live" in probe[-1]
    assert "$$status" in probe[-1]
    assert "curl" not in probe


def test_diagnostics_selects_project_and_never_requests_environment(tmp_path, monkeypatch):
    config = infra.prepare(tmp_path)
    identity = "a" * 64
    ps = [{"ID": identity, "Service": "langfuse-web", "Name": "repolaunch-integrations-langfuse-web-1", "State": "restarting", "Command": "must not be exported"},
          {"ID": "b" * 64, "Service": "redis", "State": "running", "Command": config["REDIS_PASSWORD"]}]
    compose = Mock(return_value=json.dumps(ps))
    calls = []

    def docker(arguments, supplied_config, timeout=600, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[0] == "info":
            return '{"memory_total":8589934592,"cpu_count":4}'
        if arguments[0] == "inspect":
            assert arguments[-1] == identity
            assert ".Config.Env" not in arguments[arguments.index("--format") + 1]
            return json.dumps({"state": {"OOMKilled": True, "Error": config["POSTGRES_PASSWORD"]}, "restart_count": 2, "memory_limit": 2147483648, "exposed_ports": {"3000/tcp": {}}, "entrypoint": ["dumb-init"], "command": ["node", "web/server.js"]})
        if arguments[0] == "logs":
            assert arguments == ["logs", "--tail", "120", identity]
            assert kwargs == {"include_stderr": True}
            return "password=" + config["LANGFUSE_SECRET_KEY"] + " public=" + config["LANGFUSE_PUBLIC_KEY"] + "X" * 18000
        raise AssertionError("Diagnostic attempted unexpected operation: " + str(arguments))

    monkeypatch.setattr(infra, "compose_command", compose)
    monkeypatch.setattr(infra, "docker_command", docker)
    path = infra.collect_diagnostics(tmp_path)
    result = json.loads(path.read_text(encoding="utf-8"))
    compose.assert_called_once_with(tmp_path, ["ps", "--all", "--format", "json"], config, 15)
    assert [call[0][0] for call in calls] == ["info", "inspect", "logs"]
    assert result["containers"][0]["details"]["state"]["OOMKilled"] is True
    assert result["docker_resources"]["cpu_count"] == 4
    assert "Command" not in result["containers"][0]
    assert "logs" not in result["containers"][1]
    content = path.read_text(encoding="utf-8")
    assert len(content) > 12000  # Bounded logs must not truncate the JSON document.
    for key in infra.SECRET_KEYS + ["LANGFUSE_PUBLIC_KEY"]:
        assert config[key] not in content


def test_diagnostics_writes_useful_report_when_docker_unavailable(tmp_path, monkeypatch):
    infra.prepare(tmp_path)
    monkeypatch.setattr(infra, "docker_command", Mock(side_effect=RuntimeError("daemon unavailable")))
    monkeypatch.setattr(infra, "compose_command", Mock(side_effect=RuntimeError("compose unavailable")))
    result = json.loads(infra.collect_diagnostics(tmp_path).read_text(encoding="utf-8"))
    assert len(result["errors"]) == 2
    assert result["containers"] == []


def test_deployment_failure_collects_diagnostics_without_restart(monkeypatch, capsys):
    monkeypatch.setattr(infra, "deploy", Mock(side_effect=RuntimeError("web unhealthy")))
    collect = Mock(return_value=Path("safe-diagnostics.json"))
    monkeypatch.setattr(infra, "collect_diagnostics", collect)
    assert infra.main([]) == 1
    collect.assert_called_once_with(reason="web unhealthy")
    assert "safe-diagnostics.json" in capsys.readouterr().err
