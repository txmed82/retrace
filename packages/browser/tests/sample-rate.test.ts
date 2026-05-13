/**
 * P2.2 — browser SDK `sampleRate` contract.
 *
 * `init({ sampleRate })` exists today; this test pins the
 * behavior so future refactors of `start()` / `flush()` can't
 * silently re-enable capture for clients that opted in at
 * `sampleRate: 0`.
 *
 * Contract:
 *   1. `sampleRate: 1` (default) records.
 *   2. `sampleRate: 0` does NOT call rrweb.record().
 *   3. `sampleRate: 0` also short-circuits manual `client.start()`.
 *   4. `sampleRate` out-of-range values clamp to the safe end
 *      (>=1 → on, <=0 → off).
 *
 * Per the breadcrumbs test pattern, we mock rrweb so jsdom doesn't
 * have to boot a full recorder; we assert against the mock via
 * `vi.mocked(record)` instead of inspecting network traffic.
 */

import { describe, expect, it, beforeEach, vi } from "vitest";

// Inline mock — `vi.mock` is hoisted to the top of the file, so
// the factory can't close over an outer variable. Define the mock
// inline and retrieve it via `vi.mocked()` in the test body.
vi.mock("rrweb", () => ({
  record: vi.fn(() => () => {
    /* stop fn — no-op */
  }),
}));

import { record } from "rrweb";
import { init } from "../src/index.js";

const FAKE_KEY = "rtpk_test_key";

beforeEach(() => {
  vi.mocked(record).mockClear();
  document.body.innerHTML = "";
});

describe("sampleRate", () => {
  it("records by default (sampleRate omitted = 100%)", () => {
    init({ apiKey: FAKE_KEY });
    expect(record).toHaveBeenCalled();
  });

  it("records when sampleRate is exactly 1", () => {
    init({ apiKey: FAKE_KEY, sampleRate: 1 });
    expect(record).toHaveBeenCalled();
  });

  it("does not record when sampleRate is 0", () => {
    init({ apiKey: FAKE_KEY, sampleRate: 0 });
    expect(record).not.toHaveBeenCalled();
  });

  it("manual start() is a no-op when sampled out", () => {
    const client = init({
      apiKey: FAKE_KEY,
      autoStart: false,
      sampleRate: 0,
    });
    // Even when the host calls start() explicitly, sampleRate=0
    // must keep rrweb dormant — otherwise the opt-out is fake.
    client.start();
    expect(record).not.toHaveBeenCalled();
  });

  it("clamps sampleRate >= 1 to always-record", () => {
    init({ apiKey: FAKE_KEY, sampleRate: 1.5 });
    expect(record).toHaveBeenCalled();
  });

  it("clamps sampleRate <= 0 to never-record", () => {
    init({ apiKey: FAKE_KEY, sampleRate: -0.1 });
    expect(record).not.toHaveBeenCalled();
  });

  it("partial sample rate produces deterministic behavior under stubbed RNG", () => {
    // sampleRate is non-deterministic in production (uses
    // Math.random); pin behavior at both ends of the random range
    // by stubbing the RNG.
    const random = vi.spyOn(Math, "random");

    // 0.99 < 0.5? false → not sampled in.
    random.mockReturnValueOnce(0.99);
    init({ apiKey: FAKE_KEY, sampleRate: 0.5 });
    expect(record).not.toHaveBeenCalled();

    // 0.1 < 0.5? true → sampled in.
    vi.mocked(record).mockClear();
    random.mockReturnValueOnce(0.1);
    init({ apiKey: FAKE_KEY, sampleRate: 0.5 });
    expect(record).toHaveBeenCalled();

    random.mockRestore();
  });
});
