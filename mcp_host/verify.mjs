#!/usr/bin/env node
/* Real loopback discovery check, driven by the same official SDK as the host. */
import net from "node:net";
import { spawn } from "node:child_process";
import process from "node:process";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

function option(name) { const i = process.argv.indexOf(name); return i < 0 ? null : process.argv[i + 1]; }
const root = option("--root");
if (!root) throw new Error("usage: verify.mjs --root PROJECT_ROOT");
const reservation = net.createServer();
await new Promise((resolve) => reservation.listen(0, "127.0.0.1", resolve));
const port = reservation.address().port;
await new Promise((resolve) => reservation.close(resolve));
const hostFile = new URL("./megaprog-mcp-host.mjs", import.meta.url).pathname;
const child = spawn(process.execPath, [hostFile, "--root", root, "--port", String(port)], { stdio: ["ignore", "ignore", "pipe"] });
let hostDiagnostics = "";
let childFailure = null;
child.stderr.setEncoding("utf8");
child.stderr.on("data", (chunk) => { hostDiagnostics += chunk; });
child.on("error", (error) => { childFailure = error; });
const waitForChildExit = () => child.exitCode !== null ? Promise.resolve() : new Promise((resolve) => {
  const timeout = setTimeout(resolve, 1000);
  child.once("exit", () => { clearTimeout(timeout); resolve(); });
});
let verified = null;
let preCloseSdkErrors = [];
let closeStarted = false;
const clientRequestMethods = [];
const clientResponseTypes = [];
const isExpectedCloseAbort = (error) => error?.name === "AbortError";
try {
  let health;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (childFailure) throw childFailure;
    try { health = await (await fetch(`http://127.0.0.1:${port}/health`)).json(); if (health.ok) break; } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!health || JSON.stringify(health.tools) !== JSON.stringify(["get_review_batch", "submit_review_batch"]) ||
      health.resources.length || health.prompts.length) throw new Error("health contract is not exact");
  const client = new Client({ name: "MegaProg verification", version: "2.0.7" });
  // SDK 1.30 handles JSON Streamable HTTP responses. Recording actual client
  // traffic proves this check does not open an incompatible SSE lifecycle.
  const sdkFetch = async (input, init) => {
    const method = init?.method || (input instanceof Request ? input.method : "GET");
    clientRequestMethods.push(method);
    const response = await fetch(input, init);
    clientResponseTypes.push(response.headers.get("content-type") || "");
    return response;
  };
  const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`), { fetch: sdkFetch });
  transport.onerror = (error) => {
    // Closing the SDK client aborts its SSE fetch.  That AbortError is an
    // expected post-close lifecycle event.  Every SDK error before close (and
    // every other error while closing) must remain visible to the verifier.
    if (!closeStarted || !isExpectedCloseAbort(error)) preCloseSdkErrors.push(error);
  };
  await client.connect(transport);
  const listing = await client.listTools();
  const names = listing.tools.map((tool) => tool.name);
  const capabilities = client.getServerCapabilities();
  if (JSON.stringify(names) !== JSON.stringify(["get_review_batch", "submit_review_batch"]) ||
      !capabilities.tools || capabilities.resources || capabilities.prompts) throw new Error("discovery exposed a capability outside the fixed contract");
  const batch = await client.callTool({ name: "get_review_batch", arguments: { max_items: 1, max_bytes: 12000 } });
  if (batch.isError || !batch.content?.[0] || batch.content[0].type !== "text") {
    throw new Error("get_review_batch did not return an advisory result through the SDK client");
  }
  let invalidRejected = false;
  // Schema-valid but semantically impossible: exercise the real advisory
  // rejection path without producing an SDK schema-validation diagnostic.
  try {
    const invalid = await client.callTool({ name: "submit_review_batch", arguments: {
      contract_version: "megaprog-review-v1", task_id: "invalid",
      task_hash: "0".repeat(64), bundle_id: "0".repeat(24),
      repo_state_hash: "0".repeat(64), advisory: {}
    } });
    invalidRejected = Boolean(invalid.isError);
  } catch (_) {
    invalidRejected = true;
  }
  if (!invalidRejected) throw new Error("submit_review_batch accepted an invalid SDK request");
  if (clientRequestMethods.some((method) => method !== "POST") ||
      clientResponseTypes.some((type) => type.includes("text/event-stream"))) {
    throw new Error("official SDK client did not remain in the supported JSON Streamable HTTP mode");
  }
  closeStarted = true;
  await client.close();
  await new Promise((resolve) => setImmediate(resolve));
  verified = { tools: names, resources: [], prompts: [] };
} finally {
  child.kill();
  await waitForChildExit();
}
// Client.connect sends notifications/initialized. Failing to retain and route
// its session used to surface here as HTTP 500. Reject every pre-close SDK
// error and every host transport/server diagnostic; the invalid submit uses
// the normal advisory rejection path, so no diagnostic is hidden.
if (preCloseSdkErrors.length || /MegaProg MCP (transport SDK|server|request) error:/.test(hostDiagnostics)) {
  throw new Error("SDK client or host stderr reported a Streamable HTTP lifecycle error");
}
process.stdout.write(JSON.stringify(verified));
