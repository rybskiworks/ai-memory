"""Check the installed native package with only temporary, provider-free state."""

import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time


def main():
    package, version, ca_bundle = sys.argv[1:]
    binary = str(Path(package) / "bin/ai-memory")
    assert (Path(package) / "share/ai-memory/hooks").is_dir()
    assert (Path(package) / "etc/ai-memory/config.default.toml").is_file()

    with tempfile.TemporaryDirectory(prefix="ai-memory-native-") as directory:
        root = Path(directory).resolve()
        home = root / "home"
        home.mkdir()
        data = root / "data"
        # Do not inherit provider credentials, user configuration, proxies, or
        # the caller's project identity. The check only connects to loopback.
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "AI_MEMORY_HOME": str(home),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_STATE_HOME": str(root / "state"),
            "TMPDIR": str(root),
            "SSL_CERT_FILE": ca_bundle,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "NO_COLOR": "1",
        }
        for args in (["--version"], ["--help"], ["completions", "bash"]):
            result = subprocess.run(
                [binary, *args], cwd=root, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, check=True, timeout=15,
            )
            if args == ["--version"]:
                assert result.stdout.strip() == f"ai-memory {version}", result.stdout
        assert not data.exists()
        assert not list(home.iterdir()), "pure CLI commands created user state"

        token = secrets.token_hex(32)
        config = root / "server.toml"
        config.write_text(
            'bind = "127.0.0.1:0"\n'
            'allowed_hosts = ["127.0.0.1"]\n'
            'embedding_provider = "none"\n'
            'consolidate_on_session_end = false\n'
            '[maintenance]\nenabled = false\n'
            '[auth]\n'
            f'bearer_token = "{token}"\n'
        )
        log_path = root / "server.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [binary, "--data-dir", str(data), "--config", str(config),
                 "serve", "--transport", "http", "--no-watcher", "--enable-web"],
                cwd=root, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                # Let the server allocate the port, avoiding a bind/close race.
                # Only parse the ready line emitted after its listener binds.
                deadline = time.monotonic() + 45
                port = None
                while time.monotonic() < deadline:
                    assert process.poll() is None, "server exited before readiness"
                    for line in log_path.read_text().splitlines():
                        if "MCP HTTP server ready" in line:
                            match = re.search(r"127\.0\.0\.1:(\d+)", line)
                            if match:
                                port = int(match[1])
                    if port:
                        break
                    time.sleep(0.05)
                assert port, "server did not announce a bound loopback listener"

                def request(path, payload=None, bearer=token, host="127.0.0.1"):
                    headers = {"Host": host, "Accept": "application/json, text/event-stream"}
                    if bearer is not None:
                        headers["Authorization"] = f"Bearer {bearer}"
                    body = None
                    if payload is not None:
                        headers["Content-Type"] = "application/json"
                        body = json.dumps(payload).encode()
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    try:
                        connection.request("POST" if payload is not None else "GET", path, body, headers)
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()

                for bearer in (None, "incorrect-package-check-token"):
                    status, _ = request("/admin/status", bearer=bearer)
                    assert status == 401, f"invalid bearer returned {status}"
                status, _ = request("/admin/status", host="untrusted.invalid")
                assert status == 403, f"untrusted Host returned {status}"
                status, body = request("/admin/status")
                assert status == 200, (status, body)
                report = json.loads(body)
                assert report["version"] == version, report
                assert Path(report["data_dir"]) == data, report
                assert Path(report["db_path"]).is_relative_to(data), report
                for provider in ("llm", "embedding"):
                    assert report["providers"][provider]["status"] == "disabled", report

                def rpc(number, method, params):
                    status, body = request("/mcp", {
                        "jsonrpc": "2.0", "id": number, "method": method, "params": params,
                    })
                    assert status == 200, (method, status, body)
                    response = json.loads(body)
                    assert response.get("id") == number and "error" not in response, response
                    result = response["result"]
                    assert not result.get("isError", False), result
                    return result

                initialized = rpc(1, "initialize", {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "native-package-check", "version": "1"},
                })
                assert "tools" in initialized["capabilities"], initialized
                listing = rpc(2, "tools/list", {})
                names = {tool["name"] for tool in listing["tools"]}
                assert {"memory_write_page", "memory_query"} <= names, names
                scope = {"workspace": "package-check", "project": "native-service"}
                rpc(3, "tools/call", {"name": "memory_write_page", "arguments": {
                    **scope, "path": "notes/native-package.md",
                    "body": "# Native package\n\nHermeticquartz persists without providers.",
                }})
                query = rpc(4, "tools/call", {"name": "memory_query", "arguments": {
                    **scope, "query": "Hermeticquartz",
                }})
                assert "native-package.md" in json.dumps(query), query
                assert not (data / "models").exists(), "disabled embeddings created model state"
                status, css = request("/web/static/tailwind.css")
                assert status == 200 and len(css) > 100, "embedded CSS is missing"
                status, logo = request("/web/static/logo.png")
                assert status == 200 and logo.startswith(b"\x89PNG\r\n\x1a\n"), "embedded logo is missing"
            except BaseException:
                print(log_path.read_text().replace(token, "<redacted>"), file=sys.stderr)
                raise
            finally:
                # Stop only the process started above; never discover or kill
                # a server by port/name, and always reap it before deleting data.
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        assert process.returncode == 0, f"server did not stop cleanly: {process.returncode}"
        print("Native version/assets, isolated HTTP auth, MCP and provider-free FTS passed")


if __name__ == "__main__":
    main()
