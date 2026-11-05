"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "chrome-extension", "scripts", "bridge-api.js"),
  "utf8",
);

function loadApi(fetch) {
  const context = { fetch };
  vm.runInNewContext(source, context);
  return context.Watch6RBridgeApi;
}

test("sends the local token and returns a valid bridge response", async () => {
  let request;
  const api = loadApi(async (url, options) => {
    request = { url, options };
    return { ok: true, status: 200, json: async () => ({ ok: true, data: { state: "connected" } }) };
  });

  const response = await api.request("http://127.0.0.1:8765", "local-token", "/api/status");
  assert.equal(response.data.state, "connected");
  assert.equal(request.url, "http://127.0.0.1:8765/api/status");
  assert.equal(request.options.headers["X-Oraimo-Token"], "local-token");
  assert.equal(request.options.cache, "no-store");
});

test("reports invalid local API token errors clearly", async () => {
  const api = loadApi(async () => ({
    ok: false,
    status: 401,
    json: async () => ({ ok: false, error: "missing or invalid bridge token" }),
  }));
  await assert.rejects(
    api.request("http://127.0.0.1:8765", "bad-token", "/api/status"),
    /bridge rejected the token/i,
  );
});

test("reports server errors and malformed response bodies", async () => {
  const serverErrorApi = loadApi(async () => ({
    ok: false,
    status: 503,
    json: async () => ({ ok: false, error: "BLE unavailable" }),
  }));
  await assert.rejects(
    serverErrorApi.request("http://127.0.0.1:8765", "token", "/api/status"),
    /BLE unavailable/,
  );

  const malformedApi = loadApi(async () => ({
    ok: true,
    status: 200,
    json: async () => {
      throw new SyntaxError("invalid JSON");
    },
  }));
  await assert.rejects(
    malformedApi.request("http://127.0.0.1:8765", "token", "/api/status"),
    /invalid response/i,
  );
});

test("reports a stopped local bridge", async () => {
  const api = loadApi(async () => {
    throw new TypeError("fetch failed");
  });
  await assert.rejects(
    api.request("http://127.0.0.1:8765", "token", "/api/status"),
    /local bridge request failed/i,
  );
});
