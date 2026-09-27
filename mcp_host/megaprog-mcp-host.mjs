#!/usr/bin/env node
/* Official @modelcontextprotocol/sdk Streamable HTTP host for MegaProg.
 * It has exactly two SDK tool registrations.  All work is delegated through
 * stdin/stdout to ai_dev.mcp_bridge, which owns the safe Python boundary.
 */
import http from "node:http";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";

const TOOL_NAMES = ["get_review_batch", "submit_review_batch"];
const MAX_BRIDGE_LINE = 1024 * 1024;
// MegaProg has no server-pushed messages. Select the documented SDK JSON
// response mode instead of maintaining an idle SSE lifecycle.
const JSON_RESPONSE_MODE = true;

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index < 0 ? fallback : process.argv[index + 1];
}
const root = option("--root", null);
const host = option("--host", "127.0.0.1");
const port = Number(option("--port", "8000"));
const python = option("--python", "python3");
if (!root || !Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("usage: megaprog-mcp-host.mjs --root PROJECT_ROOT [--host HOST] [--port PORT]");
}
if (!["127.0.0.1", "::1", "localhost"].includes(host)) {
  throw new Error("MCP host may bind only to a loopback address; publish HTTPS with a separately configured reverse proxy");
}

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const bridge = spawn(python, ["-m", "ai_dev.mcp_bridge", "--root", root], {
  cwd: root, stdio: ["pipe", "pipe", "inherit"], shell: false,
  env: { ...process.env, PYTHONPATH: sourceRoot + (process.env.PYTHONPATH ? path.delimiter + process.env.PYTHONPATH : "") }
});
let pending = null;
let buffer = "";
bridge.stdout.setEncoding("utf8");
bridge.stdout.on("data", (chunk) => {
  buffer += chunk;
  const lineEnd = buffer.indexOf("\n");
  if (lineEnd < 0) return;
  const line = buffer.slice(0, lineEnd); buffer = buffer.slice(lineEnd + 1);
  const current = pending; pending = null;
  if (!current) return;
  try { current.resolve(JSON.parse(line)); } catch (error) { current.reject(error); }
});
bridge.on("exit", (code) => {
  if (pending) { pending.reject(new Error(`advisory bridge exited (${code})`)); pending = null; }
});

function bridgeCall(tool, arguments_) {
  if (!TOOL_NAMES.includes(tool) || pending) return Promise.reject(new Error("invalid or concurrent bridge request"));
  const payload = JSON.stringify({ tool, arguments: arguments_ });
  if (Buffer.byteLength(payload) > MAX_BRIDGE_LINE) return Promise.reject(new Error("bridge request exceeds byte limit"));
  return new Promise((resolve, reject) => {
    pending = { resolve, reject };
    bridge.stdin.write(payload + "\n", "utf8", (error) => { if (error && pending) { pending = null; reject(error); } });
  }).then((reply) => {
    if (!reply.ok) throw new Error(reply.error || "advisory bridge rejected request");
    return reply.result;
  });
}
function result(value) { return { content: [{ type: "text", text: JSON.stringify(value) }] }; }
function failure(error) { return { content: [{ type: "text", text: JSON.stringify({ error: String(error.message || error) }) }], isError: true }; }

async function readBoundedJson(request) {
  const length = request.headers["content-length"];
  if (typeof length !== "string" || !/^(0|[1-9][0-9]*)$/.test(length) || Number(length) > MAX_BRIDGE_LINE) {
    throw Object.assign(new Error("MCP request requires a bounded Content-Length"), { statusCode: 413 });
  }
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let bytes = 0;
  let text = "";
  for await (const chunk of request) {
    bytes += chunk.length;
    if (bytes > MAX_BRIDGE_LINE) throw Object.assign(new Error("MCP request exceeds byte limit"), { statusCode: 413 });
    text += decoder.decode(chunk, { stream: true });
  }
  text += decoder.decode();
  if (bytes !== Number(length)) throw new Error("MCP request body length does not match Content-Length");
  try { return JSON.parse(text); }
  catch (_) { throw new Error("MCP request body is not valid JSON"); }
}

const sessions = new Map();

function createServer() {
  const server = new McpServer({ name: "MegaProg advisory", version: "2.0.7" }, {
    instructions: "Untrusted non-blocking advisory only. Exactly two bounded review tools are available."
  });
  server.tool("get_review_batch", "Return a bounded, read-only MegaProg advisory review batch.", {
    max_items: z.number().int().min(1).optional(), max_bytes: z.number().int().min(256).optional()
  }, async (arguments_) => { try { return result(await bridgeCall("get_review_batch", arguments_)); } catch (error) { return failure(error); } });
  server.tool("submit_review_batch", "Store one validated untrusted advisory response for an issued review batch.", {
    contract_version: z.string().min(1), task_id: z.string().min(1), task_hash: z.string().regex(/^[0-9a-f]{64}$/),
    bundle_id: z.string().regex(/^[0-9a-f]{24}$/), repo_state_hash: z.string().regex(/^[0-9a-f]{64}$/), advisory: z.unknown()
  }, async (arguments_) => { try { return result(await bridgeCall("submit_review_batch", arguments_)); } catch (error) { return failure(error); } });
  server.onerror = (error) => process.stderr.write(
    `MegaProg MCP server error: ${error instanceof Error ? error.name : "UnknownError"}\n`
  );
  return server;
}

function isInitializeRequest(body) {
  return Boolean(body && !Array.isArray(body) && body.jsonrpc === "2.0" && body.method === "initialize");
}

function isInitializedNotification(body) {
  // This is deliberately narrower than a generic notification: only the
  // lifecycle acknowledgement is answered outside the SDK transport.
  return Boolean(body && !Array.isArray(body) && body.jsonrpc === "2.0" &&
    body.method === "notifications/initialized" && !Object.hasOwn(body, "id"));
}

function sendJsonNotificationAcknowledgement(response) {
  // SDK 1.30 turns a 202 response to notifications/initialized into a GET/SSE
  // attempt.  MegaProg is JSON-only, so acknowledge this one valid, routed
  // notification directly with 200 JSON and do not start that lifecycle.
  response.writeHead(200, { "content-type": "application/json" });
  response.end(JSON.stringify({}));
}

async function createSessionTransport() {
  let transport;
  transport = new StreamableHTTPServerTransport({
    // SDK 1.30 routes all requests after initialize by Mcp-Session-Id. A
    // stateful transport is required for Client's initialized notification.
    sessionIdGenerator: () => randomUUID(),
    // The official StreamableHTTPClientTransport accepts this JSON mode.
    enableJsonResponse: JSON_RESPONSE_MODE,
    onsessioninitialized: (sessionId) => sessions.set(sessionId, transport)
  });
  // Some transport failures are emitted instead of thrown by handleRequest.
  // Keep this diagnostic metadata-only: SDK errors may include request details.
  transport.onerror = (error) => process.stderr.write(
    `MegaProg MCP transport SDK error: ${error instanceof Error ? error.name : "UnknownError"}\n`
  );
  transport.onclose = () => {
    if (transport.sessionId) sessions.delete(transport.sessionId);
  };
  await createServer().connect(transport);
  return transport;
}
const listener = http.createServer(async (request, response) => {
  const url = new URL(request.url || "/", `http://${request.headers.host || host}`);
  if (request.method === "GET" && url.pathname === "/health") {
    response.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
    response.end(JSON.stringify({ ok: true, endpoint: "/mcp", tools: TOOL_NAMES, resources: [], prompts: [] })); return;
  }
  if (url.pathname !== "/mcp") { response.writeHead(404); response.end(); return; }
  try {
    if (JSON_RESPONSE_MODE && request.method === "GET") {
      // GET SSE is optional for Streamable HTTP. An explicit 405 keeps this
      // endpoint JSON-only; SDK 1.30 recognizes 405 as an expected outcome.
      response.writeHead(405, { "allow": "POST", "content-type": "application/json" });
      response.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message: "SSE is not enabled" }, id: null })); return;
    }
    if (request.method === "POST") {
      // SDK 1.30's Node API accepts a pre-parsed body as its third argument.
      // Supplying it is required after this bounded read: the wrapped Web Request
      // body is single-use, and trying to parse it again produces a transport 500.
      let parsedBody;
      try { parsedBody = await readBoundedJson(request); }
      catch (error) {
        const status = error?.statusCode === 413 ? 413 : 400;
        response.writeHead(status, { "content-type": "application/json" });
        response.end(JSON.stringify({ error: status === 413 ? "MCP request exceeds byte limit" : "MCP request body is invalid" })); return;
      }
      const sessionId = request.headers["mcp-session-id"];
      if (typeof sessionId === "string") {
        const transport = sessions.get(sessionId);
        if (!transport) {
          response.writeHead(404, { "content-type": "application/json" });
          response.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32001, message: "Session not found" }, id: null })); return;
        }
        if (JSON_RESPONSE_MODE && isInitializedNotification(parsedBody)) {
          sendJsonNotificationAcknowledgement(response);
          return;
        }
        await transport.handleRequest(request, response, parsedBody);
        return;
      }
      if (!isInitializeRequest(parsedBody)) {
        response.writeHead(400, { "content-type": "application/json" });
        response.end(JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message: "Session ID required" }, id: null })); return;
      }
      await (await createSessionTransport()).handleRequest(request, response, parsedBody);
      return;
    }
    const sessionId = request.headers["mcp-session-id"];
    const transport = typeof sessionId === "string" ? sessions.get(sessionId) : undefined;
    if (!transport) { response.writeHead(404); response.end(); return; }
    await transport.handleRequest(request, response);
  } catch (error) {
    process.stderr.write(`MegaProg MCP request error: ${error instanceof Error ? error.name : "UnknownError"}\n`);
    if (!response.headersSent) response.writeHead(500, { "content-type": "application/json" });
    if (!response.writableEnded) response.end(JSON.stringify({ error: "MCP transport failed" }));
  }
});
listener.listen(port, host, () => process.stderr.write(`MegaProg MCP listening on http://${host}:${port}/mcp\n`));
function close() { listener.close(); for (const transport of sessions.values()) transport.close(); bridge.kill(); }
process.on("SIGINT", close); process.on("SIGTERM", close);
