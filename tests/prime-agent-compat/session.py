#!/usr/bin/env python3
"""Check two real Prime sessions against an isolated native memory service."""

import argparse
import json
import subprocess

import run as common


def preflight(args):
    common.preflight(args)
    if args.timeout < 20:
        raise common.PrerequisiteError("the session check requires a timeout between 20 and 60 seconds")
    for relative in (
        "packages/coding-agent/test/suite/harness.ts",
        "packages/coding-agent/test/utilities.ts",
        "packages/coding-agent/src/core/agent-session.ts",
        "packages/coding-agent/src/core/extensions/runner.ts",
        "packages/ai/src/providers/faux.ts",
    ):
        source = common.absolute_file(args.prime_source / relative)
        built = common.absolute_file(args.prime_tree / relative)
        if source.read_bytes() != built.read_bytes():
            raise common.PrerequisiteError(f"Prime session source disagrees with pin: {relative}")
    for relative in (
        "node_modules/tsx/dist/loader.mjs", "packages/ai/dist/index.js",
        "packages/agent/dist/index.js",
    ):
        common.absolute_file(args.prime_tree / relative)
    manifest = common.absolute_file(args.prime_tree / "node_modules/tsx/package.json")
    if json.loads(manifest.read_text())["version"] != "4.23.1":
        raise common.PrerequisiteError("expected the reviewed tsx 4.23.1")


def combined_status(loader, session, exit_code):
    status = common.loader_status(session, exit_code)
    if loader["status"] == "error" or status == "error":
        return "error"
    return "passed" if loader["status"] == "passed" and status == "passed" else "failed"


def run_session(args, root, env, extension, url):
    # Keep the original loader contract visible, including unsupported events.
    report = common.run_loader(args, root, env, extension, url)
    if report["status"] == "error" or report["loader"].get("loader_errors"):
        return report
    config = root / "session-tsconfig.json"
    config.write_text(json.dumps({"compilerOptions": {
        "target": "ES2022", "module": "NodeNext", "moduleResolution": "NodeNext",
    }}))
    session_env = {**env, "TSX_TSCONFIG_PATH": str(config), "TSX_DISABLE_CACHE": "1"}
    result_file = root / "session-result.json"
    input_file = root / "session-input.json"
    input_file.write_text(json.dumps({
        "prime_tree": str(args.prime_tree), "extension": str(extension),
        "project": str(root / "project"), "server_url": url,
        "result_file": str(result_file), "budget_ms": (args.timeout - 15) * 1000,
    }))
    try:
        child = common.command([
            args.node, "--import", args.prime_tree / "node_modules/tsx/dist/loader.mjs",
            common.ROOT / "session.mjs", input_file,
        ], root / "project", session_env, args.timeout)
    except subprocess.CalledProcessError as error:
        child = error
    except subprocess.TimeoutExpired:
        report.update(status="error", session_capture="incomplete", session={
            "passed": False, "harness_error": "session child exceeded its deadline and was killed/reaped",
            "stages": {"capture": "incomplete", "recall": "incomplete", "cleanup": "failed"},
        })
        return report
    token = env["AI_MEMORY_AUTH_TOKEN"]
    (root / "session.log").write_text((child.stdout + child.stderr).replace(token, "<redacted>"))
    if not result_file.is_file():
        raise RuntimeError("session child exited without a completed result")
    result = common.redact(json.loads(result_file.read_text()), token)
    report.update(
        session=result, session_exit=child.returncode,
        session_capture=result["stages"].get("capture", "not_run"),
        status=combined_status(report, result, child.returncode),
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("ai-memory", "ai-memory-revision", "prime-source", "prime-tree", "node", "nix", "ca-bundle"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    try:
        preflight(args)  # Also check session-only inputs before allocating state.
        report = common.execute(args, check=run_session)
    except common.PrerequisiteError as error:
        report = {"status": "blocked", "error": str(error), "session_capture": "not_run", "real_client_cli": "not_run"}
    print(json.dumps(report, indent=2))
    return {"passed": 0, "failed": 1, "blocked": 2, "error": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
