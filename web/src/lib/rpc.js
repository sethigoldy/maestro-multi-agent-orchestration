// JSON-RPC calls to the daemon (POST /), the same methods the CLI and the MCP
// server use, so every action shares one code path. The daemon refuses a POST
// from any page but its own (see docs/design-console.md, section 3).
import { authHeaders } from "./auth.js";

let nextId = 1;

export async function rpc(method, params, fetchImpl = fetch) {
  const res = await fetchImpl("/", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json", ...authHeaders() },
    body: JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params }),
  });
  let body;
  try {
    body = await res.json();
  } catch {
    throw new Error(`the daemon answered HTTP ${res.status}`);
  }
  if (body && body.error) {
    throw new Error(typeof body.error === "string" ? body.error : body.error.message || `the daemon answered HTTP ${res.status}`);
  }
  return body ? body.result : undefined;
}
