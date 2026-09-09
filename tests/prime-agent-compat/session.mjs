import assert from "node:assert/strict";
import { randomBytes, randomUUID } from "node:crypto";
import { readFile, writeFile, copyFile } from "node:fs/promises";
import { join, resolve, relative, isAbsolute } from "node:path";
import { pathToFileURL } from "node:url";
import { WORKSPACE, PROJECT, bounded, cleanupSession, decodeContent, capturedPrompt, recalledObservation, queryRequest } from "./session-support.mjs";

const delay = (ms) => new Promise((done) => setTimeout(done, ms));

async function run(input) {
  const result = {
    passed: false, stages: { capture: "not_run", recall: "not_run", cleanup: "not_run" },
    session_capture: "not_run", real_client_cli: "not_run", shutdown_drain_guarantee: "not_tested",
    requests: [], diagnostic_requests: [], sessions: [], extension_errors: [], guard_errors: [],
  };
  const deadline = Date.now() + input.budget_ms;
  const remaining = () => {
    const ms = deadline - Date.now();
    assert.ok(ms > 0, "session case exceeded its deadline");
    return ms;
  };
  const nativeFetch = globalThis.fetch;
  const expectedOrigin = new URL(input.server_url).origin;
  const token = process.env.AI_MEMORY_AUTH_TOKEN;
  assert.ok(token, "fixture auth token is absent");
  const live = [];
  let emitSessionShutdownEvent;
  let stage = "setup";
  const requestTimeout = 3000;

  function completedTurn(harness, expectedCalls) {
    assert.equal(harness.faux.state.callCount, expectedCalls);
    assert.equal(harness.getPendingResponseCount(), 0);
    const replies = harness.session.messages.filter((message) => message.role === "assistant");
    assert.equal(replies.length, expectedCalls, "expected all real faux responses in session history");
    assert.ok(replies.every((message) => ["stop", "toolUse"].includes(message.stopReason) && !message.errorMessage),
      "real session reported a model error or incomplete response");
    assert.ok(harness.events.some((event) => event.type === "agent_end"), "real session did not finish its turn");
  }

  // This observes real transport. It never supplies canned hook/MCP responses.
  globalThis.fetch = async (resource, options = {}) => {
    try {
      const url = new URL(resource instanceof Request ? resource.url : String(resource));
      assert.equal(url.origin, expectedOrigin, "unexpected extension network origin");
      const method = options.method ?? "GET";
      assert.ok((url.pathname === "/handoff" && method === "GET") ||
        (["/hook", "/mcp"].includes(url.pathname) && method === "POST"), "unexpected extension endpoint/method");
      const body = typeof options.body === "string" ? JSON.parse(options.body) : undefined;
      const headers = new Headers(options.headers);
      const observed = {
        path: url.pathname, method, rpc_method: body?.method,
        actor_session_id: headers.get("X-Memory-Actor-Session-Id"),
      };
      if (url.pathname === "/mcp") {
        assert.ok(["initialize", "notifications/initialized", "tools/list", "tools/call"].includes(body?.method));
        if (body.method === "tools/call") {
          assert.equal(body.params?.name, "memory_query", "fixture may call only the real read query tool");
          observed.tool_name = body.params.name;
          observed.tool_arguments = body.params.arguments;
        }
      } else if (url.pathname === "/hook") {
        assert.equal(url.searchParams.get("agent"), "prime-agent");
        assert.equal(url.searchParams.get("workspace"), WORKSPACE);
        assert.equal(url.searchParams.get("project"), PROJECT);
        observed.event = url.searchParams.get("event");
        observed.session_id = body?.sessionID;
        observed.cwd = body?.cwd;
      }
      result.requests.push(observed);
      const response = await nativeFetch(resource, options);
      observed.status = response.status;
      return response;
    } catch (error) {
      result.guard_errors.push(String(error));
      throw error;
    }
  };

  // Explicit scoped reads are storage diagnostics, never the client recall.
  async function observations(sessionId, timeout = Math.min(requestTimeout, remaining())) {
    const response = await nativeFetch(`${input.server_url}/mcp`, {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream",
        Authorization: `Bearer ${token}` },
      body: JSON.stringify({ jsonrpc: "2.0", id: randomUUID(), method: "tools/call", params: {
        name: "memory_read_session_observations", arguments: {
          workspace: WORKSPACE, project: PROJECT, session_id: sessionId, limit: 50,
        },
      } }), signal: AbortSignal.timeout(timeout),
    });
    result.diagnostic_requests.push({ session_id: sessionId, status: response.status });
    assert.equal(response.status, 200, "native observation diagnostic failed HTTP auth/transport");
    const rpc = await response.json();
    if (rpc.error || rpc.result?.isError) return undefined; // Ingress may not have committed yet.
    return decodeContent(rpc.result?.content);
  }

  async function poll(label, check) {
    let last;
    while (remaining() > 0) {
      try {
        const value = await check();
        if (value) return value;
      } catch (error) {
        last = String(error);
      }
      await delay(Math.min(50, remaining()));
    }
    throw new Error(`${label} was not observed: ${last ?? "no result"}`);
  }

  try {
    const primeImport = (path) => import(pathToFileURL(join(input.prime_tree, path)).href);
    const { createHarness } = await primeImport("packages/coding-agent/test/suite/harness.ts");
    const { createTestResourceLoader } = await primeImport("packages/coding-agent/test/utilities.ts");
    const { loadExtensions } = await primeImport("packages/coding-agent/src/core/extensions/loader.ts");
    ({ emitSessionShutdownEvent } = await primeImport("packages/coding-agent/src/core/extensions/runner.ts"));
    const { fauxAssistantMessage, fauxToolCall } = await primeImport("packages/ai/dist/index.js");
    stage = "session_setup";

    async function create(label) {
      // Separate copies have byte-identical installer content, while avoiding
      // module-instance or mutable queue sharing between the two live sessions.
      const extension = join(input.project, `ai-memory-${label}.ts`);
      await copyFile(input.extension, extension);
      const loaded = await loadExtensions([extension], input.project);
      assert.equal(loaded.extensions.length, 1);
      assert.deepEqual(loaded.errors, []);
      const harness = await createHarness({
        api: `compat-${label}-${randomUUID()}`, provider: `compat-${label}-${randomUUID()}`,
        models: [{ id: `compat-${label}`, contextWindow: 128000, maxTokens: 128 }],
        resourceLoader: createTestResourceLoader({ extensionsResult: loaded }),
        tools: [], persistSession: true,
        settings: { autoRefine: { enabled: false }, compaction: { enabled: false },
          retry: { enabled: false }, agentTraces: { enabled: false }, telemetry: { enabled: false } },
      });
      const entry = { harness, label, shutdown: false };
      live.push(entry);
      const subpath = relative(resolve(process.env.TMPDIR), resolve(harness.tempDir));
      assert.ok(subpath && !subpath.startsWith("..") && !isAbsolute(subpath), "Prime allocated outside fixture TMPDIR");
      assert.equal(harness.sessionManager.getCwd(), harness.tempDir);
      await writeFile(join(harness.tempDir, ".ai-memory.toml"), `workspace = "${WORKSPACE}"\nproject = "${PROJECT}"\n`);
      const id = harness.sessionManager.getSessionId();
      assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
      result.sessions.push({ label, id, cwd: harness.tempDir, provider: harness.getModel().provider });
      await harness.session.bindExtensions({ onError: (error) => result.extension_errors.push(String(error.message ?? error)) });
      // Diagnostic readiness only; the existing loader report preserves the
      // unsupported event and this does not assert first-prompt readiness.
      await poll(`${label} memory query registration`, async () =>
        harness.session.getAllTools().some((tool) => tool.name === "memory_query"));
      harness.session.setActiveToolsByName(["memory_query"]);
      assert.deepEqual(harness.session.getActiveToolNames(), ["memory_query"]);
      return entry;
    }

    const canary = `primecanary${randomBytes(12).toString("hex")}`;
    const a = await create("a");
    const aId = a.harness.sessionManager.getSessionId();
    stage = "capture";
    result.stages.capture = "running";
    a.harness.setResponses([fauxAssistantMessage("Acknowledged the local fixture fact.")]);
    // Keep the canary out of the first-line log title: this test must reach
    // raw-observation FTS, not accidentally match a generated log page.
    await bounded(a.harness.session.promptAndWait(`Record a harmless fixture detail.\nThe calibration word is ${canary}.`), remaining(), "capture turn");
    completedTurn(a.harness, 1);
    const captured = await poll("committed prompt", async () => {
      const data = await observations(aId);
      return data && capturedPrompt(data, aId, a.harness.tempDir, canary);
    });
    result.captured_observation = { id: captured.id, session_id: captured.session_id, kind: captured.kind };
    result.stages.capture = "passed";
    result.session_capture = "passed";

    const b = await create("b");
    const bId = b.harness.sessionManager.getSessionId();
    assert.notEqual(aId, bId);
    assert.notEqual(a.harness.getModel().provider, b.harness.getModel().provider);
    stage = "recall";
    result.stages.recall = "running";
    const callId = `recall-${randomUUID()}`;
    const arguments_ = { query: canary, limit: 5 };
    let modelVisibleHit;
    let responseError;
    b.harness.setResponses([
      fauxAssistantMessage(fauxToolCall("memory_query", arguments_, { id: callId }), { stopReason: "toolUse" }),
      (context) => {
        try {
          modelVisibleHit = recalledObservation(context.messages, callId, captured, canary);
        } catch (error) {
          responseError = error;
          throw error;
        }
        return fauxAssistantMessage("The prior session's fixture fact was retrieved.");
      },
    ]);
    await bounded(b.harness.session.promptAndWait("Retrieve the earlier session's calibration detail using memory."), remaining(), "recall turn");
    if (responseError) throw responseError;
    assert.ok(modelVisibleHit, "no real model-visible query result was checked");
    completedTurn(b.harness, 2);
    const hit = recalledObservation(b.harness.session.messages, callId, captured, canary);
    result.recalled_observation = { id: hit.id, session_id: hit.session_id, kind: hit.kind };
    queryRequest(result.requests, bId, arguments_);
    assert.deepEqual(result.extension_errors, []);
    assert.deepEqual(result.guard_errors, []);
    result.stages.recall = "passed";
    result.passed = true;
  } catch (error) {
    result.stages[stage] = "failed";
    if (stage === "setup") result.harness_error = String(error);
    else result.failure = String(error);
  } finally {
    result.stages.cleanup = "running";
    const cleanupErrors = [];
    // These waits observe eventual healthy-service ingress, not a promise that
    // the adapter's shutdown callback drained or durably spooled its queue.
    for (const entry of live) {
      const errors = await cleanupSession(entry.harness, {
        shutdown: async () => {
          if (emitSessionShutdownEvent && !entry.shutdown) {
            entry.shutdown = true;
            await emitSessionShutdownEvent(entry.harness.session.extensionRunner,
              { type: "session_shutdown", reason: "quit" });
          }
        },
        observeEnd: async () => {
          const closeDeadline = Date.now() + 2000;
          let closed;
          let ended;
          do {
            const data = await observations(entry.harness.sessionManager.getSessionId(), Math.max(1, closeDeadline - Date.now()));
            closed = data?.session?.ended_at && data.observations?.some((row) => row.kind === "session-end");
            if (closed) ended = data;
            if (!closed) await delay(25);
          } while (!closed && Date.now() < closeDeadline);
          assert.ok(closed, "native session end was not eventually committed");
          Object.assign(result.sessions.find((session) => session.label === entry.label), {
            model_calls: entry.harness.faux.state.callCount,
            ended_at: ended.session.ended_at,
            terminal_observation_id: ended.observations.find((row) => row.kind === "session-end").id,
            session_file: entry.harness.sessionManager.getSessionFile(),
          });
        },
      });
      cleanupErrors.push(...errors.map((error) => ({ session: entry.label, ...error })));
    }
    if (result.guard_errors.length) cleanupErrors.push("transport guard reported errors");
    if (cleanupErrors.length) {
      result.cleanup_errors = cleanupErrors;
      result.harness_error = "session cleanup failed";
      result.passed = false;
      result.stages.cleanup = "failed";
    } else {
      result.stages.cleanup = "passed";
    }
    // Keep the guard installed while the generated adapter may still deliver
    // queued requests; restoring ambient fetch would remove that restriction.
  }
  return result;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  const input = JSON.parse(await readFile(process.argv[2], "utf8"));
  let result;
  try {
    result = await run(input);
  } catch (error) {
    result = { passed: false, harness_error: String(error), stages: {}, session_capture: "not_run", real_client_cli: "not_run" };
  }
  await writeFile(input.result_file, JSON.stringify(result, null, 2) + "\n", { mode: 0o600 });
  process.exitCode = result.passed ? 0 : 1;
}
