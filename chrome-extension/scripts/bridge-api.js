"use strict";

(function exposeBridgeApi(global) {
  class BridgeApiError extends Error {
    constructor(message, status = null) {
      super(message);
      this.name = "BridgeApiError";
      this.status = status;
    }
  }

  async function request(baseUrl, token, path, options = {}) {
    let response;
    try {
      response = await global.fetch(`${baseUrl}${path}`, {
        cache: "no-store",
        ...options,
        headers: {
          "X-Oraimo-Token": token,
          ...(options.headers || {}),
        },
      });
    } catch (error) {
      throw new BridgeApiError(
        `Local bridge request failed; check that the bridge is running and the exact extension origin is configured: ${error.message}`,
      );
    }

    let payload;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }

    if (response.status === 401) {
      throw new BridgeApiError(
        "The bridge rejected the token. Check Settings and the token printed by the bridge.",
        response.status,
      );
    }
    if (!response.ok) {
      throw new BridgeApiError(
        payload?.error || `Bridge request failed with HTTP ${response.status}.`,
        response.status,
      );
    }
    if (!payload || payload.ok !== true) {
      throw new BridgeApiError(payload?.error || "The bridge returned an invalid response.");
    }
    return payload;
  }

  global.Watch6RBridgeApi = { BridgeApiError, request };
})(globalThis);
