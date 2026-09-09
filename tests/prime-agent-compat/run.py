#!/usr/bin/env python3
"""Exercise installer output with an explicitly supplied, pinned Prime loader."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import time


PRIME_REVISION = "d1b072686d6b7b1b7d2ad773541e33aba1f578d9"
PRIME_NAR_HASH = "sha256-MwAnWnAusPCiFburJdiDWOIBPAAqeHCb0Ob5+vlUKJY="
NODE_VERSION = "v24.15.0"
ROOT = Path(__file__).resolve().parent


class PrerequisiteError(RuntimeError):
    """The exact inputs are absent; no compatibility claim can be made."""


def absolute_file(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise PrerequisiteError(f"required absolute file is unavailable: {path}")
    return path.resolve()


def preflight(args):
    if not re.fullmatch(r"[0-9a-f]{40}", args.ai_memory_revision):
        raise PrerequisiteError("ai-memory revision must be a complete immutable Git SHA")
    for field in ("ai_memory", "node", "nix", "ca_bundle"):
        setattr(args, field, absolute_file(getattr(args, field)))
    for field in ("prime_source", "prime_tree"):
        path = Path(getattr(args, field))
        if not path.is_absolute() or not path.is_dir():
            raise PrerequisiteError(f"required {field} is unavailable: {path}; build it separately, no install fallback")
        setattr(args, field, path.resolve())
    required = [
        "package.json", "node_modules/typescript/lib/typescript.js",
        "packages/coding-agent/dist/core/extensions/loader.js",
    ]
    for relative in required:
        absolute_file(args.prime_tree / relative)
    for relative in ("package.json", "packages/coding-agent/src/core/extensions/loader.ts",
                     "packages/coding-agent/src/core/extensions/types.ts"):
        source = absolute_file(args.prime_source / relative)
        built_source = absolute_file(args.prime_tree / relative)
        if source.read_bytes() != built_source.read_bytes():
            raise PrerequisiteError(f"Prime built-tree source disagrees with pin: {relative}")
    if json.loads((args.prime_source / "package.json").read_text())["version"] != "0.7.1":
        raise PrerequisiteError("unexpected Prime package version")
    if not 1 <= args.timeout <= 60:
        raise PrerequisiteError("timeout must be between 1 and 60 seconds")


def isolated_environment(root, args):
    return {
        "PATH": os.pathsep.join([str(args.ai_memory.parent), str(args.node.parent), "/usr/bin", "/bin"]),
        "HOME": str(root / "home"), "AI_MEMORY_HOME": str(root / "memory-home"),
        "PRIME_AGENT_CODING_AGENT_DIR": str(root / "prime"),
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "xdg-data"), "XDG_STATE_HOME": str(root / "state"),
        "TMPDIR": str(root / "tmp"), "SSL_CERT_FILE": str(args.ca_bundle),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "NO_COLOR": "1", "NIX_CONFIG": "experimental-features = nix-command flakes",
    }


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def redact(value, token):
    return json.loads(json.dumps(value).replace(token, "<redacted>"))


def loader_status(result, exit_code):
    if "harness_error" in result:
        return "error"
    return "passed" if result.get("passed") and exit_code == 0 else "failed"


def stop_owned(process):
    """Stop/reap this exact Popen child, not a discovered server or daemon."""
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return process.returncode


def command(argv, root, env, timeout):
    return subprocess.run(
        list(map(str, argv)), cwd=root, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout, check=True,
    )


def wait_ready(process, log_path, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("native service exited before readiness")
        for line in log_path.read_text().splitlines():
            if "MCP HTTP server ready" in line:
                match = re.search(r"127\.0\.0\.1:(\d+)", line)
                if match:
                    return int(match[1])
        time.sleep(0.05)
    raise RuntimeError("native service did not bind its loopback listener before deadline")


def server_status(port, token):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/admin/status", headers={"Host": "127.0.0.1", "Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"native service status returned HTTP {response.status}")
        return json.loads(response.read())
    finally:
        connection.close()


def execute(args):
    preflight(args)  # Missing builds must fail before any service/config creation.
    root = Path(tempfile.mkdtemp(prefix="ai-memory-prime-compat-")).resolve()
    for directory in ("home", "memory-home", "prime", "config", "cache", "xdg-data", "state", "tmp", "project"):
        (root / directory).mkdir()
    env = isolated_environment(root, args)
    token = secrets.token_hex(32)
    report = {
        "status": "error", "artifacts": str(root),
        "prime_revision": PRIME_REVISION, "prime_source": str(args.prime_source),
        "prime_tree": str(args.prime_tree), "node": str(args.node),
        "ai_memory": str(args.ai_memory), "ai_memory_declared_revision": args.ai_memory_revision,
        "ai_memory_sha256": sha256(args.ai_memory),
        "session_capture": "not_run", "real_client_cli": "not_run",
    }
    process = None
    try:
        observed_hash = command([args.nix, "hash", "path", args.prime_source], root, env, args.timeout).stdout.strip()
        if observed_hash != PRIME_NAR_HASH:
            raise PrerequisiteError("Prime source NAR hash does not match the reviewed revision")
        report["prime_source_nar_hash"] = observed_hash
        node_version = command([args.node, "--version"], root, env, args.timeout).stdout.strip()
        if node_version != NODE_VERSION:
            raise PrerequisiteError(f"expected Node {NODE_VERSION}, observed {node_version}")
        report["node_version"] = node_version
        report["ai_memory_version"] = command([args.ai_memory, "--version"], root, env, args.timeout).stdout.strip()
        config = root / "server.toml"
        config.write_text(
            'bind = "127.0.0.1:0"\nallowed_hosts = ["127.0.0.1"]\n'
            'embedding_provider = "none"\nconsolidate_on_session_end = false\n'
            '[maintenance]\nenabled = false\n[auth]\n'
            f'bearer_token = "{token}"\n'
        )
        config.chmod(0o600)
        data = root / "data"
        log_path = root / "server.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [str(args.ai_memory), "--data-dir", str(data), "--config", str(config),
                 "serve", "--transport", "http", "--no-watcher", "--enable-web"],
                cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            )
            port = wait_ready(process, log_path, args.timeout)
            status = server_status(port, token)
            if Path(status["data_dir"]) != data or not Path(status["db_path"]).is_relative_to(data):
                raise RuntimeError("service used data outside its fixture")
            if any(status["providers"][name]["status"] != "disabled" for name in ("llm", "embedding")):
                raise RuntimeError("native service enabled a provider")
            report["native_service"] = {"ready": True, "providers_disabled": True}
            env["AI_MEMORY_AUTH_TOKEN"] = token
            extension = root / "prime" / "extensions" / "ai-memory-prime-agent.ts"
            url = f"http://127.0.0.1:{port}"
            installed = command([
                args.ai_memory, "--data-dir", data, "--config", config,
                "install-hooks", "--agent", "prime-agent", "--apply",
                "--config-file", extension, "--server-url", url,
            ], root / "project", env, args.timeout)
            (root / "installer.log").write_text((installed.stdout + installed.stderr).replace(token, "<redacted>"))
            report["extension_sha256"] = sha256(extension)
            input_file = root / "loader-input.json"
            result_file = root / "loader-result.json"
            input_file.write_text(json.dumps({
                "prime_source": str(args.prime_source), "prime_tree": str(args.prime_tree),
                "extension": str(extension), "project": str(root / "project"),
                "server_url": url, "result_file": str(result_file),
                "discovery_timeout_ms": max(100, (args.timeout - 2) * 1000),
            }))
            try:
                loaded = command([args.node, ROOT / "loader.mjs", input_file], root / "project", env, args.timeout)
                report["loader_exit"] = loaded.returncode
            except subprocess.CalledProcessError as error:
                loaded = error
                report["loader_exit"] = error.returncode
            (root / "loader.log").write_text((loaded.stdout + loaded.stderr).replace(token, "<redacted>"))
            if not result_file.is_file():
                raise RuntimeError("loader exited without a completed result")
            report["loader"] = redact(json.loads(result_file.read_text()), token)
            report["status"] = loader_status(report["loader"], report["loader_exit"])
            if (data / "models").exists():
                raise RuntimeError("disabled embeddings created model state")
    except PrerequisiteError as error:
        report.update(status="blocked", error=str(error))
    except Exception as error:
        report.update(status="error", error=str(error))
    finally:
        if process is not None:
            try:
                report["service_exit"] = stop_owned(process)
                if report["service_exit"] != 0:
                    report["status"] = "error"
            except Exception as error:
                report.update(status="error", cleanup_error=str(error))
        report = redact(report, token)
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("ai-memory", "ai-memory-revision", "prime-source", "prime-tree", "node", "nix", "ca-bundle"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        report = execute(args)
    except PrerequisiteError as error:
        report = {"status": "blocked", "error": str(error), "session_capture": "not_run", "real_client_cli": "not_run"}
    print(json.dumps(report, indent=2))
    return {"passed": 0, "failed": 1, "blocked": 2, "error": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
