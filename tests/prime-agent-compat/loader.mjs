import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

export function assessExtension(extensions, errors, supportedEvents) {
  const subscriptions = [...new Set(extensions.flatMap((extension) => [...extension.handlers.keys()]))].sort();
  const tools = [...new Set(extensions.flatMap((extension) => [...extension.tools.keys()]))].sort();
  const unsupported = subscriptions.filter((name) => !supportedEvents.has(name));
  const missingReads = ["memory_query", "memory_read_page"].filter((name) => !tools.includes(name));
  return {
    loaded_extensions: extensions.length,
    loader_errors: errors,
    subscriptions,
    unsupported_subscriptions: unsupported,
    registered_tools: tools,
    missing_read_tools: missingReads,
    passed: extensions.length === 1 && errors.length === 0 && unsupported.length === 0 && missingReads.length === 0,
  };
}

function supportedEvents(ts, text) {
  const source = ts.createSourceFile("types.ts", text, ts.ScriptTarget.Latest, true);
  assert.equal(source.parseDiagnostics.length, 0, "Prime API declarations did not parse");
  const api = source.statements.find((node) => ts.isInterfaceDeclaration(node) && node.name.text === "ExtensionAPI");
  assert.ok(api, "pinned source has no ExtensionAPI interface");
  const events = new Set();
  for (const member of api.members) {
    if (!ts.isMethodSignature(member) || member.name.getText(source) !== "on") continue;
    const type = member.parameters[0]?.type;
    assert.ok(type && ts.isLiteralTypeNode(type) && ts.isStringLiteral(type.literal),
      "Prime on() declaration changed; review the compatibility contract");
    events.add(type.literal.text);
  }
  assert.ok(events.has("session_start") && events.has("refine_complete"), "unexpected Prime event API");
  return events;
}

async function run(input) {
  const requests = [];
  const nativeFetch = globalThis.fetch;
  const expectedOrigin = new URL(input.server_url).origin;
  // Observe the actual transport, never manufacture an MCP response. The
  // generated adapter uses fetch; reject its unexpected destinations before IO.
  globalThis.fetch = async (resource, options) => {
    const url = new URL(resource instanceof Request ? resource.url : String(resource));
    assert.equal(url.origin, expectedOrigin, "extension attempted a non-fixture origin");
    assert.ok(["/mcp", "/hook", "/handoff"].includes(url.pathname), "unexpected extension endpoint");
    let rpcMethod;
    if (typeof options?.body === "string") {
      try { rpcMethod = JSON.parse(options.body).method; } catch { /* Non-RPC hook payload. */ }
    }
    const observation = { path: url.pathname, method: options?.method ?? "GET", rpc_method: rpcMethod };
    requests.push(observation);
    const response = await nativeFetch(resource, options);
    observation.status = response.status;
    return response;
  };
  let result;
  {
    const require = createRequire(join(input.prime_tree, "package.json"));
    const ts = require("typescript");
    const apiText = await readFile(join(input.prime_source, "packages/coding-agent/src/core/extensions/types.ts"), "utf8");
    const events = supportedEvents(ts, apiText);
    const modulePath = join(input.prime_tree, "packages/coding-agent/dist/core/extensions/loader.js");
    const { loadExtensions } = await import(pathToFileURL(modulePath).href);
    assert.equal(typeof loadExtensions, "function", "actual Prime loader is unavailable");
    const loaded = await loadExtensions([input.extension], input.project);
    // The generated factory currently starts asynchronous discovery without
    // returning its promise. This bounded observation is not a first-prompt
    // readiness assertion and must not be presented as one.
    const deadline = Date.now() + input.discovery_timeout_ms;
    do {
      result = assessExtension(loaded.extensions, loaded.errors, events);
      if (loaded.errors.length || !result.missing_read_tools.length) break;
      await new Promise((done) => setTimeout(done, 25));
    } while (Date.now() < deadline);
    result = { ...result, requests, session_capture: "not_run", real_client_cli: "not_run" };
  }
  return result;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  const input = JSON.parse(await readFile(process.argv[2], "utf8"));
  let result;
  try {
    result = await run(input);
  } catch (error) {
    result = { passed: false, harness_error: String(error), session_capture: "not_run", real_client_cli: "not_run" };
  }
  await writeFile(input.result_file, JSON.stringify(result, null, 2) + "\n", { mode: 0o600 });
  process.exitCode = result.passed ? 0 : 1;
}
