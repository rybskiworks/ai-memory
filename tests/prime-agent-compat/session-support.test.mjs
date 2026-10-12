import assert from "node:assert/strict";
import test from "node:test";
import { capturedPrompt, recalledObservation, queryRequest, cleanupSession } from "./session-support.mjs";

const canary = "primecanaryfixture";
const captured = { id: "observation-a", session_id: "session-a", kind: "user-prompt", body: canary };
const hit = { ...captured, snippet: `The word is ${canary}.` };
const capture = () => ({
  session: { session_id: "session-a", cwd: "/fixture/a", agent_kind: "prime-agent", ended_at: null },
  elided_other_scope: 0, observations: [captured],
});
const toolResult = (overrides = {}) => ({
  role: "toolResult", toolCallId: "query-b", toolName: "memory_query", isError: false,
  content: [{ type: "text", text: JSON.stringify({ hits: [], raw_hits: [hit] }) }], ...overrides,
});

test("capture evidence binds the actual session, cwd, agent and observation", () => {
  assert.equal(capturedPrompt(capture(), "session-a", "/fixture/a", canary).id, captured.id);
  for (const change of [
    { session_id: "session-b" }, { cwd: "/ambient" }, { agent_kind: "other" }, { ended_at: "now" },
  ]) {
    const result = capture();
    Object.assign(result.session, change);
    assert.throws(() => capturedPrompt(result, "session-a", "/fixture/a", canary));
  }
});

test("only the matching actual tool result can prove recall", () => {
  assert.equal(recalledObservation([toolResult()], "query-b", captured, canary).id, captured.id);
  assert.throws(() => recalledObservation([{ role: "user", content: canary }], "query-b", captured, canary));
  assert.throws(() => recalledObservation([toolResult({ toolCallId: "other" })], "query-b", captured, canary));
  assert.throws(() => recalledObservation([toolResult({ isError: true })], "query-b", captured, canary));
});

test("page hits and a different session's canary cannot impersonate raw recall", () => {
  for (const body of [
    { hits: [{ snippet: canary }], raw_hits: [hit] },
    { hits: [], raw_hits: [{ ...hit, id: "different-observation" }] },
    { hits: [], raw_hits: [{ ...hit, session_id: "session-b" }] },
    { hits: [], raw_hits: [{ ...hit, snippet: "missing" }] },
  ]) {
    const message = toolResult({ content: [{ type: "text", text: JSON.stringify(body) }] });
    assert.throws(() => recalledObservation([message], "query-b", captured, canary));
  }
});

test("bridge evidence requires the current B id and unmodified scope-free query", () => {
  const args = { query: canary, limit: 5 };
  const request = { rpc_method: "tools/call", tool_name: "memory_query", actor_session_id: "session-b", tool_arguments: args, status: 200 };
  assert.equal(queryRequest([request], "session-b", args), request);
  assert.throws(() => queryRequest([{ ...request, actor_session_id: "session-a" }], "session-b", args));
  assert.throws(() => queryRequest([{ ...request, tool_arguments: { ...args, project: "guessed" } }], "session-b", args));
  assert.throws(() => queryRequest([request, request], "session-b", args));
});

function cleanupFixture(failure) {
  const order = [];
  const step = (name) => {
    order.push(name);
    if (failure === name) throw new Error(`${name} failed`);
  };
  return {
    order,
    harness: {
      session: { disposeAsync: async () => step("dispose") },
      faux: { unregister: () => step("unregister") },
      cleanup: () => { throw new Error("must never delete fixture evidence"); },
    },
    operations: { shutdown: async () => step("shutdown"), observeEnd: async () => step("session_end") },
  };
}

test("failed shutdown still observes end, awaits disposal and unregisters without deletion", async () => {
  const fixture = cleanupFixture("shutdown");
  const errors = await cleanupSession(fixture.harness, fixture.operations);
  assert.deepEqual(fixture.order, ["shutdown", "session_end", "dispose", "unregister"]);
  assert.deepEqual(errors.map((error) => error.stage), ["shutdown"]);
});

test("failed storage observation and disposal do not skip later cleanup steps", async () => {
  for (const failure of ["session_end", "dispose", "unregister"]) {
    const fixture = cleanupFixture(failure);
    const errors = await cleanupSession(fixture.harness, fixture.operations);
    assert.deepEqual(fixture.order, ["shutdown", "session_end", "dispose", "unregister"]);
    assert.deepEqual(errors.map((error) => error.stage), [failure]);
  }
});

test("successful cleanup waits for asynchronous disposal before unregistering", async () => {
  const fixture = cleanupFixture();
  fixture.harness.session.disposeAsync = async () => {
    await new Promise((done) => setTimeout(done, 1));
    fixture.order.push("dispose");
  };
  assert.deepEqual(await cleanupSession(fixture.harness, fixture.operations), []);
  assert.deepEqual(fixture.order, ["shutdown", "session_end", "dispose", "unregister"]);
});

test("a stalled shutdown is bounded and does not suppress disposal", async () => {
  const fixture = cleanupFixture();
  fixture.operations.shutdown = () => { fixture.order.push("shutdown"); return new Promise(() => {}); };
  const errors = await cleanupSession(fixture.harness, { ...fixture.operations, timeout: 5 });
  assert.deepEqual(fixture.order, ["shutdown", "session_end", "dispose", "unregister"]);
  assert.deepEqual(errors.map((error) => error.stage), ["shutdown"]);
  assert.match(errors[0].error, /deadline/);
});
