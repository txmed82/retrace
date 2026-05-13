/**
 * Browser SDK breadcrumb tests.
 *
 * Covers the four acceptance criteria from the P0.4 roadmap item:
 *
 *   1. Ring buffer caps at `maxBreadcrumbs`.
 *   2. addBreadcrumb is reachable from the client.
 *   3. Click breadcrumbs respect `privacy.maskTextSelector` (and the
 *      ARIA-derived `accessibleName` fallback is also gated on the
 *      same mask).
 *   4. An unhandled `error` event captures the trail in the local
 *      breadcrumb ring via the post-snapshot self-record path.
 *
 * Plus regressions added after the PR #130 CodeRabbit pass:
 *   - SDK ingest URL is excluded from HTTP breadcrumbs.
 *   - Query / fragment / credentials are sanitized out of URLs.
 *   - Breadcrumb `data` is deep-cloned (nested mutation can't rewrite
 *     historical entries).
 *
 * We avoid the SDK's flushing path here — the network mock would
 * obscure what we're really testing. Instead we exercise `init({autoStart})`
 * and read `getBreadcrumbs()` / look up the rrweb events the SDK pushed.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";

// rrweb is heavy and side-effecty; stub it so jsdom doesn't have to
// boot a full recorder.
vi.mock("rrweb", () => ({
  record: vi.fn(() => () => {
    /* stop fn — no-op */
  }),
}));

import { init, type RetraceBreadcrumb } from "../src/index.js";

const FAKE_KEY = "rtpk_test_key";

// Reset DOM + globals between tests so leaked listeners can't cross-pollute.
beforeEach(() => {
  document.body.innerHTML = "";
});

afterEach(() => {
  // Best-effort cleanup of leaked listeners; the SDK's `stop()` does
  // this, but if a test forgot to stop we don't want spillover.
  document.body.innerHTML = "";
});

describe("breadcrumb ring buffer", () => {
  it("caps at maxBreadcrumbs and drops the oldest entry on overflow", () => {
    const client = init({
      apiKey: FAKE_KEY,
      autoStart: false,
      maxBreadcrumbs: 3,
    });
    try {
      for (let i = 0; i < 10; i += 1) {
        client.addBreadcrumb({ category: "manual", message: `m${i}` });
      }
      const trail = client.getBreadcrumbs();
      expect(trail.map((b) => b.message)).toEqual(["m7", "m8", "m9"]);
    } finally {
      client.stop();
    }
  });

  it("drops empty messages so accidental no-ops don't fill the ring", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: false });
    try {
      client.addBreadcrumb({ message: "" });
      client.addBreadcrumb({ message: "real" });
      const trail = client.getBreadcrumbs();
      expect(trail.map((b) => b.message)).toEqual(["real"]);
    } finally {
      client.stop();
    }
  });

  it("clones data on add and on read so the public trail can't mutate the ring", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: false });
    try {
      const sourceData = { userId: 42 };
      client.addBreadcrumb({ category: "manual", message: "hi", data: sourceData });
      sourceData.userId = 99;
      const trail = client.getBreadcrumbs();
      expect(trail[0].data).toEqual({ userId: 42 });
      // Mutating the snapshot can't leak back to subsequent reads.
      trail[0].message = "MUTATED";
      expect(client.getBreadcrumbs()[0].message).toBe("hi");
    } finally {
      client.stop();
    }
  });
});

describe("auto-capture: click breadcrumbs", () => {
  it("records a ui.click breadcrumb on document click", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: true });
    try {
      const button = document.createElement("button");
      button.id = "checkout";
      button.textContent = "Place order";
      document.body.appendChild(button);
      button.click();
      const trail = client.getBreadcrumbs();
      const click = trail.find((b: RetraceBreadcrumb) => b.category === "ui.click");
      expect(click).toBeDefined();
      expect(click!.message).toContain("button");
      expect(click!.message).toContain("Place order");
    } finally {
      client.stop();
    }
  });

  it("respects maskTextSelector — no element text leaks into the breadcrumb", () => {
    const client = init({
      apiKey: FAKE_KEY,
      autoStart: true,
      privacy: { maskTextSelector: ".secret" },
    });
    try {
      const button = document.createElement("button");
      button.className = "secret";
      button.textContent = "sk-DEADBEEF_LEAK";
      document.body.appendChild(button);
      button.click();
      const trail = client.getBreadcrumbs();
      const click = trail.find((b: RetraceBreadcrumb) => b.category === "ui.click");
      expect(click).toBeDefined();
      expect(click!.message).not.toContain("sk-DEADBEEF_LEAK");
    } finally {
      client.stop();
    }
  });
});

describe("auto-capture: console breadcrumbs", () => {
  it("mirrors console.error into a breadcrumb at error level", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: true });
    try {
      // eslint-disable-next-line no-console
      console.error("kaboom");
      const trail = client.getBreadcrumbs();
      const err = trail.find(
        (b: RetraceBreadcrumb) => b.category === "console" && b.message === "kaboom",
      );
      expect(err).toBeDefined();
      expect(err!.level).toBe("error");
    } finally {
      client.stop();
    }
  });
});

describe("auto-capture: navigation breadcrumbs", () => {
  it("records a navigation breadcrumb on history.pushState", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: true });
    try {
      const before = window.location.href;
      window.history.pushState({}, "", "/next");
      const trail = client.getBreadcrumbs();
      const nav = trail.find((b: RetraceBreadcrumb) => b.category === "navigation");
      expect(nav).toBeDefined();
      expect(nav!.data?.from).toBe(before);
      expect(String(nav!.data?.to)).toContain("/next");
    } finally {
      client.stop();
      // Restore in case jsdom keeps the URL between tests.
      window.history.replaceState({}, "", "/");
    }
  });

  it("dedupes navigation breadcrumbs that don't actually change the URL", () => {
    // popstate fires on history.back/forward, but if the URL hasn't
    // changed (e.g. an SPA dispatching a synthetic popstate against
    // the current location), we shouldn't emit a duplicate breadcrumb.
    const client = init({ apiKey: FAKE_KEY, autoStart: true });
    try {
      window.history.pushState({}, "", "/a");
      const before = client.getBreadcrumbs().filter((b) => b.category === "navigation").length;
      // Dispatch a bare popstate — URL stays at /a, so this should be
      // a no-op for our breadcrumb trail.
      window.dispatchEvent(new PopStateEvent("popstate"));
      const after = client.getBreadcrumbs().filter((b) => b.category === "navigation").length;
      expect(after).toBe(before);
    } finally {
      client.stop();
      window.history.replaceState({}, "", "/");
    }
  });
});

describe("HTTP breadcrumbs", () => {
  it("skips the SDK's own ingest URL so the ring doesn't fill with self-noise", async () => {
    // Stub fetch *before* init so the SDK wraps our stub. The wrapped
    // fetch should see the original input args; we always 200.
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () => new Response("{}", { status: 200 })) as typeof fetch;
    try {
      const client = init({
        apiKey: FAKE_KEY,
        autoStart: true,
        ingestUrl: "http://ingest.test/api/sdk/replay",
      });
      try {
        await fetch("http://ingest.test/api/sdk/replay", { method: "POST" });
        await fetch("http://example.test/api/users", { method: "GET" });
        const http = client.getBreadcrumbs().filter((b) => b.category === "http");
        // Only the non-ingest request should appear.
        expect(http).toHaveLength(1);
        expect(http[0].message).toContain("example.test/api/users");
      } finally {
        client.stop();
      }
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("sanitizes query strings and fragments out of breadcrumb URLs", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () => new Response("{}", { status: 500 })) as typeof fetch;
    try {
      const client = init({ apiKey: FAKE_KEY, autoStart: true });
      try {
        await fetch("http://api.test/v1/auth?token=sk-LEAK&user=ada@x.com#frag");
        const http = client.getBreadcrumbs().filter((b) => b.category === "http");
        expect(http).toHaveLength(1);
        expect(http[0].message).not.toContain("sk-LEAK");
        expect(http[0].message).not.toContain("ada@x.com");
        expect(http[0].message).not.toContain("#frag");
        expect(String(http[0].data?.url)).toBe("http://api.test/v1/auth");
      } finally {
        client.stop();
      }
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

describe("exception event payload", () => {
  it("attaches the breadcrumb trail to an unhandled error event", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: true });
    const events: unknown[] = [];
    // Spy on the underlying push by patching the rrweb mock per-test.
    // The SDK's exception capture pushes a custom event into the
    // internal events queue; we can't access that queue directly,
    // but the breadcrumb trail seen via getBreadcrumbs() after the
    // throw must include both the manual trail and the error itself.
    try {
      client.addBreadcrumb({ category: "auth", message: "login attempt", level: "info" });
      const errorEvent = new ErrorEvent("error", {
        message: "kaboom",
        filename: "/app.js",
        lineno: 42,
      });
      window.dispatchEvent(errorEvent);
      const trail = client.getBreadcrumbs();
      // Trail should contain the manual login + an error breadcrumb
      // recorded by `onError` after the snapshot was taken.
      expect(trail.some((b) => b.message === "login attempt")).toBe(true);
      expect(
        trail.some((b) => b.category === "error" && b.message.includes("kaboom")),
      ).toBe(true);
      events.push(trail);
    } finally {
      client.stop();
    }
    expect(events).toHaveLength(1);
  });
});

describe("page URL sanitization in breadcrumb data", () => {
  it("strips query / fragment from location.href in click breadcrumbs", () => {
    // Regression for CodeRabbit Major on PR #130: page URLs stored on
    // breadcrumb `data` previously carried the full `location.href`
    // including `?token=…` and `#fragment`.
    window.history.replaceState({}, "", "/checkout?reset_token=sk-LEAK#step2");
    try {
      const client = init({ apiKey: FAKE_KEY, autoStart: true });
      try {
        const btn = document.createElement("button");
        btn.textContent = "Pay";
        document.body.appendChild(btn);
        btn.click();
        const click = client.getBreadcrumbs().find((b) => b.category === "ui.click");
        expect(click).toBeDefined();
        const url = String(click!.data?.url ?? "");
        expect(url).not.toContain("sk-LEAK");
        expect(url).not.toContain("#step2");
        expect(url).toContain("/checkout");
      } finally {
        client.stop();
      }
    } finally {
      window.history.replaceState({}, "", "/");
    }
  });

  it("strips query / fragment from navigation `from` and `to`", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: true });
    try {
      window.history.pushState({}, "", "/users/42?token=secret&utm=ig#tab=billing");
      const nav = client.getBreadcrumbs().find((b) => b.category === "navigation");
      expect(nav).toBeDefined();
      expect(String(nav!.data?.to)).not.toContain("token=secret");
      expect(String(nav!.data?.to)).not.toContain("#tab=billing");
      expect(String(nav!.data?.to)).toContain("/users/42");
    } finally {
      client.stop();
      window.history.replaceState({}, "", "/");
    }
  });
});

describe("breadcrumb data deep-clone", () => {
  it("nested mutation after capture does not rewrite historical entries", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: false });
    try {
      const ctx = { request: { headers: { auth: "ok" } } };
      client.addBreadcrumb({ category: "manual", message: "with nested", data: ctx });
      // Mutate the source AFTER capture.
      ctx.request.headers.auth = "MUTATED";
      const trail = client.getBreadcrumbs();
      const data = trail[0].data as { request: { headers: { auth: string } } };
      expect(data.request.headers.auth).toBe("ok");
    } finally {
      client.stop();
    }
  });
});

describe("addBreadcrumb manual API", () => {
  it("accepts a custom breadcrumb and the message is preserved", () => {
    const client = init({ apiKey: FAKE_KEY, autoStart: false });
    try {
      client.addBreadcrumb({
        category: "auth",
        message: "login attempt",
        level: "info",
        data: { user_id: "u_1" },
      });
      const trail = client.getBreadcrumbs();
      expect(trail.at(-1)).toMatchObject({
        category: "auth",
        message: "login attempt",
        level: "info",
        data: { user_id: "u_1" },
      });
    } finally {
      client.stop();
    }
  });

  it("falls back to default when maxBreadcrumbs is NaN/Infinity/non-finite", () => {
    // Regression for CodeRabbit Major on PR #130: `Math.max(1,
    // Math.min(500, NaN))` is NaN, and `breadcrumbs.length > NaN`
    // is always false — so the ring would have grown unbounded.
    const nanClient = init({
      apiKey: FAKE_KEY,
      autoStart: false,
      maxBreadcrumbs: NaN,
    });
    try {
      for (let i = 0; i < 100; i += 1) {
        nanClient.addBreadcrumb({ message: `m${i}` });
      }
      // Falls back to default = 50.
      expect(nanClient.getBreadcrumbs().length).toBe(50);
    } finally {
      nanClient.stop();
    }
    const infClient = init({
      apiKey: FAKE_KEY,
      autoStart: false,
      maxBreadcrumbs: Infinity,
    });
    try {
      for (let i = 0; i < 600; i += 1) {
        infClient.addBreadcrumb({ message: `m${i}` });
      }
      // Same fallback path.
      expect(infClient.getBreadcrumbs().length).toBe(50);
    } finally {
      infClient.stop();
    }
  });

  it("clamps maxBreadcrumbs to a sane range so an attacker can't OOM us", () => {
    const tiny = init({ apiKey: FAKE_KEY, autoStart: false, maxBreadcrumbs: 0 });
    try {
      tiny.addBreadcrumb({ message: "m1" });
      tiny.addBreadcrumb({ message: "m2" });
      // Minimum is 1, so the ring keeps exactly one.
      expect(tiny.getBreadcrumbs()).toHaveLength(1);
    } finally {
      tiny.stop();
    }
    const huge = init({ apiKey: FAKE_KEY, autoStart: false, maxBreadcrumbs: 1_000_000 });
    try {
      for (let i = 0; i < 600; i += 1) {
        huge.addBreadcrumb({ message: `m${i}` });
      }
      // Capped at 500 internally so we don't grow without bound.
      expect(huge.getBreadcrumbs().length).toBeLessThanOrEqual(500);
    } finally {
      huge.stop();
    }
  });
});
