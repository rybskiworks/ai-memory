import assert from "node:assert/strict";

export const WORKSPACE = "prime-compat";
export const PROJECT = "shared-capture";

export async function bounded(promise, ms, label) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded its deadline`)), ms);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

export async function cleanupSession(harness, { shutdown, observeEnd, timeout = 2000 }) {
  const errors = [];
  // Each step owns its error. A failed shutdown or storage diagnostic must
  // never skip actual session disposal or unregistering its faux provider.
  for (const [stage, operation] of [
    ["shutdown", shutdown], ["session_end", observeEnd],
    ["dispose", () => harness.session.disposeAsync()],
  ]) {
    try { await bounded(operation(), timeout, stage); }
    catch (error) { errors.push({ stage, error: String(error) }); }
  }
  try { harness.faux.unregister(); }
  catch (error) { errors.push({ stage: "unregister", error: String(error) }); }
  // Retain the fixture-owned directories. Upstream harness.cleanup performs
  // synchronous recursive removal with retries that can exceed our deadline.
  return errors;
}

export function decodeContent(content) {
  const text = content?.filter((part) => part.type === "text").map((part) => part.text).join("\n");
  assert.ok(text, "native tool result contains no JSON text");
  return JSON.parse(text);
}

export function capturedPrompt(result, sessionId, cwd, canary) {
  assert.equal(result.session?.session_id, sessionId, "native capture has the wrong session");
  assert.equal(result.session.agent_kind, "prime-agent", "native capture has the wrong agent");
  assert.equal(result.session.cwd, cwd, "native capture has the wrong cwd");
  assert.equal(result.session.ended_at, null, "capture session unexpectedly ended before raw recall");
  assert.equal(result.elided_other_scope, 0, "capture escaped the explicit scope");
  const observation = result.observations?.find((row) => row.kind === "user-prompt" && row.body.includes(canary));
  assert.ok(observation, "native service has no committed canary prompt");
  assert.equal(observation.session_id, sessionId);
  assert.ok(observation.id, "committed observation has no stable id");
  return observation;
}

export function recalledObservation(messages, callId, captured, canary) {
  const matches = messages.filter((message) => message.role === "toolResult" && message.toolCallId === callId);
  assert.equal(matches.length, 1, "expected one actual model-visible result for the query call");
  const message = matches[0];
  assert.equal(message.toolName, "memory_query");
  assert.equal(message.isError, false, "real memory tool execution failed");
  const result = decodeContent(message.content);
  assert.deepEqual(result.hits, [], "query used page search instead of raw observation fallback");
  const hit = result.raw_hits?.find((row) => row.id === captured.id && row.session_id === captured.session_id);
  assert.ok(hit, "query did not retrieve the exact observation committed by session A");
  assert.equal(hit.kind, "user-prompt");
  assert.ok(hit.snippet.includes(canary), "raw FTS result lacks the captured canary");
  return hit;
}

export function queryRequest(requests, sessionId, callArguments) {
  const matches = requests.filter((request) => request.rpc_method === "tools/call");
  assert.equal(matches.length, 1, "the adapter must execute exactly one MCP tool call");
  const request = matches[0];
  assert.equal(request.tool_name, "memory_query");
  assert.equal(request.actor_session_id, sessionId, "bridge forwarded a stale or missing session id");
  assert.deepEqual(request.tool_arguments, callArguments, "bridge changed current-project query arguments");
  assert.equal(request.status, 200, "actual MCP query request failed");
  return request;
}
