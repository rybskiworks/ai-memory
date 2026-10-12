import assert from "node:assert/strict";
import test from "node:test";
import { assessExtension } from "./loader.mjs";

// These are report-classification fixtures, not a substitute Prime host.
function extension(events, tools = ["memory_query", "memory_read_page"]) {
  return { handlers: new Map(events.map((event) => [event, []])), tools: new Map(tools.map((name) => [name, {}])) };
}

const supported = new Set(["session_start", "refine_complete"]);

test("an unsupported subscription is red even when read tools are present", () => {
  const report = assessExtension([extension(["session_start", "session_before_refine"])], [], supported);
  assert.equal(report.passed, false);
  assert.deepEqual(report.unsupported_subscriptions, ["session_before_refine"]);
});

test("loader errors and missing factories cannot pass", () => {
  assert.equal(assessExtension([], [], supported).passed, false);
  assert.equal(assessExtension([extension([])], [{ error: "invalid factory" }], supported).passed, false);
  assert.equal(assessExtension([extension([]), extension([])], [], supported).passed, false);
});

test("missing read tools cannot pass", () => {
  const report = assessExtension([extension(["session_start"], ["memory_query"])], [], supported);
  assert.equal(report.passed, false);
  assert.deepEqual(report.missing_read_tools, ["memory_read_page"]);
});

test("report records the actual broader tool surface without deciding its policy", () => {
  const report = assessExtension([extension(["refine_complete"], ["memory_query", "memory_read_page", "memory_write_page"])], [], supported);
  assert.equal(report.passed, true);
  assert.ok(report.registered_tools.includes("memory_write_page"));
});
