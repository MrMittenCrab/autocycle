"use strict";
const assert = require("node:assert/strict");
const { ImplementationResponse, inspectLog } = require("./implementation_response.js");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const chunk = (kind, text) => ({method:"session/update",params:{update:{sessionUpdate:kind,content:{text}}}});
const parse = events => {
  const r = new ImplementationResponse();
  events.forEach(e => r.observe(e));
  return [...r.text().matchAll(/^IMPLEMENT_STATUS: (COMPLETE|BLOCKED)\s*$/gm)].map(m=>m[1]);
};
const pieces = ["IMPLEMENT", "_", "STATUS", ":", " COMPLETE"].map(t=>chunk("agent_message_chunk",t));
assert.deepEqual(parse([chunk("agent_message_chunk","No newline."),chunk("agent_thought_chunk","Done."),...pieces]),["COMPLETE"]);
assert.deepEqual(parse([chunk("agent_message_chunk","Embedded marker: "),...pieces]),[]);
assert.deepEqual(parse([chunk("agent_message_chunk","IMPLEMENT_STATUS: COMPLETE"),chunk("tool_call","irrelevant"),chunk("agent_message_chunk","IMPLEMENT_STATUS: BLOCKED")]),["COMPLETE","BLOCKED"]);
assert.deepEqual(parse([chunk("agent_thought_chunk","IMPLEMENT_STATUS: COMPLETE")]),[]);
assert.deepEqual(parse([chunk("tool_call_update","IMPLEMENT_STATUS: COMPLETE")]),[]);
assert.deepEqual(parse([chunk("agent_message_chunk","**IMPLEMENT_STATUS: COMPLETE**")]),[]);
assert.deepEqual(parse([pieces[0],{id:99,result:{}},...pieces.slice(1)]),["COMPLETE"]);
const dir=fs.mkdtempSync(path.join(os.tmpdir(),"ac-response-test-"));
try {
  const log=path.join(dir,"cursor.log");
  const base=[{method:"cursor/update_todos",params:{todos:[{id:"1",content:"Verify work",status:"completed"}],merge:false}},chunk("agent_message_chunk","Earlier update."),chunk("agent_thought_chunk","Final."),...pieces];
  fs.writeFileSync(log,[...base,{id:4,result:{stopReason:"end_turn"}}].map(x=>JSON.stringify(x)).join("\n"));
  assert.equal(inspectLog(log).normal_complete,true);
  assert.equal(inspectLog(log).last_message,"IMPLEMENT_STATUS: COMPLETE");
  fs.writeFileSync(log,[...base,{id:4,result:{stopReason:"cancelled"}}].map(x=>JSON.stringify(x)).join("\n"));
  assert.equal(inspectLog(log).normal_complete,false);
  base[0].params.todos[0].status="in_progress";
  fs.writeFileSync(log,[...base,{id:4,result:{stopReason:"end_turn"}}].map(x=>JSON.stringify(x)).join("\n"));
  assert.equal(inspectLog(log).normal_complete,false);
} finally { fs.rmSync(dir,{recursive:true,force:true}); }
console.log("PASS message boundaries, fragmented tokens, duplicates, embedded text, log replay and incomplete outcomes");
