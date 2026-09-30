"use strict";

// ACP text chunks are fragments of a message, not independently newline-terminated
// messages. Tool/thought events delimit messages without becoming response text.
class ImplementationResponse {
  constructor() {
    this.messages = [];
    this.inMessage = false;
  }

  observe(msg) {
    const update = msg?.params?.update;
    const kind = update?.sessionUpdate;
    const text = update?.content?.text;
    if (msg?.method === "session/update" && kind === "agent_message_chunk" && typeof text === "string") {
      if (!this.inMessage) this.messages.push("");
      this.inMessage = true;
      this.messages[this.messages.length - 1] += text;
    } else if ((msg?.method === "session/update" &&
                ["agent_thought_chunk", "user_message_chunk", "tool_call", "tool_call_update", "plan"].includes(kind)) ||
               ["cursor/create_plan", "cursor/update_todos", "cursor/ask_question", "session/request_permission"].includes(msg?.method)) {
      this.inMessage = false;
    }
  }

  text() {
    return this.messages.join("\n");
  }
}

function nativeHandoff(text, tasks, finalMessage = text) {
  const lines = text.split(/\r?\n/).filter(l => l.includes("NATIVE_HANDOFF"));
  let requests = [], valid = true;
  if (lines.length) {
    try {
      if (lines.length !== 1 || !lines[0].startsWith("NATIVE_HANDOFF: ") ||
          !finalMessage.split(/\r?\n/).includes(lines[0])) throw Error();
      const value = JSON.parse(lines[0].slice("NATIVE_HANDOFF: ".length));
      if (!value || Object.keys(value).join() !== "requests" || !Array.isArray(value.requests) ||
          value.requests.some(id => typeof id !== "string" || !/^[a-f0-9]{32}$/.test(id)) ||
          new Set(value.requests).size !== value.requests.length) throw Error();
      requests = value.requests;
    } catch { valid = false; }
  }
  const delegated = tasks.filter(t => t.content === "Controller capture delegated");
  if (delegated.some(t => t.status !== "cancelled") ||
      (delegated.length > 0 && (!lines.length || !requests.length)) ||
      (requests.length > 0 && !delegated.length)) valid = false;
  return {present: lines.length > 0, valid, requests, delegated: delegated.length};
}

function inspectLog(path) {
  const fs = require("node:fs");
  const bytes = fs.readFileSync(path);
  const response = new ImplementationResponse();
  const stops = [];
  const todos = new Map();
  for (const line of bytes.toString("utf8").split(/\r?\n/)) {
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    response.observe(msg);
    if (msg?.result && Object.prototype.hasOwnProperty.call(msg.result, "stopReason")) stops.push(msg.result.stopReason);
    if (["cursor/create_plan", "cursor/update_todos"].includes(msg?.method)) {
      const merge = msg.method === "cursor/update_todos" && Boolean(msg.params?.merge);
      if (!merge) todos.clear();
      for (const todo of msg.params?.todos || []) todos.set(todo.id, {...(todos.get(todo.id) || {}), ...todo});
    }
  }
  const statuses = [...response.text().matchAll(/^IMPLEMENT_STATUS: (COMPLETE|BLOCKED)\s*$/gm)].map(m => m[1]);
  const tasks = [...todos.values()];
  const handoff = nativeHandoff(response.text(), tasks, response.messages[response.messages.length - 1] || "");
  return {
    native_handoff: handoff,
    log_sha256: require("node:crypto").createHash("sha256").update(bytes).digest("hex"),
    stop_reasons: stops,
    statuses,
    message_count: response.messages.length,
    last_message: response.messages[response.messages.length - 1] || "",
    tasks,
    normal_complete: handoff.valid && stops.length === 1 && stops[0] === "end_turn" &&
      statuses.length === 1 && statuses[0] === "COMPLETE" && tasks.length > 0 &&
      tasks.every(t => ["completed", "cancelled"].includes(t.status)) &&
      tasks.some(t => t.status === "completed")
  };
}

// Only anchored provider diagnostics qualify, never prose mentioning an error.
function transportError(text) {
  return String(text).split(/\r?\n/).some(line =>
    /^\s*(?:Error: )?(?:RetriableError:.*)?(?:ECONNRESET|ETIMEDOUT)\b/i.test(line) ||
    /^\s*(?:Error: )?RetriableError:.*(?:Premature close|http\/2 stream closed|fetch failed|PING timed out)/i.test(line) ||
    /^\s*(?:Error: )?RetriableError: \[unavailable\] Error\s*$/i.test(line) ||
    /^fatal: unable to access .*?(?:Could not resolve host|Failed to connect|Connection reset|SSL_ERROR_SYSCALL|Operation timed out)/i.test(line) ||
    /^fatal: (?:Could not resolve host|unable to connect)/i.test(line) ||
    /^ssh: (?:Could not resolve hostname|connect to host .* (?:timed out|Connection refused))/.test(line) ||
    /^Reconnecting\.\.\. \d+\/\d+ \(stream disconnected before completion: error sending request\)/.test(line));
}

module.exports = { ImplementationResponse, inspectLog, nativeHandoff, transportError };
if (require.main === module) {
  try {
    if (process.argv[2] === '--transport') {
      process.exit(transportError(require('node:fs').readFileSync(process.argv[3], 'utf8')) ? 0 : 1);
    }
    if (process.argv.length !== 3) throw new Error("Usage: node implementation_response.js <preserved-log>");
    process.stdout.write(JSON.stringify(inspectLog(process.argv[2])) + "\n");
  } catch (error) {
    process.stderr.write(String(error.message || error) + "\n");
    process.exitCode = 1;
  }
}
