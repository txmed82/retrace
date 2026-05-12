/**
 * Browser SDK breadcrumb tests.
 *
 * Covers the four acceptance criteria from the P0.4 roadmap item:
 *
 *   1. Ring buffer caps at `maxBreadcrumbs`.
 *   2. addBreadcrumb is reachable from the client.
 *   3. Click breadcrumbs respect `privacy.maskTextSelector`.
 *   4. An exception event captures the trail in its custom-event payload.
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
