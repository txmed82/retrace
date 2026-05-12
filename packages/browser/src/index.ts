import { record } from "rrweb";

type ReplayEvent = Record<string, unknown>;

export type RetracePrivacyOptions = {
  maskAllInputs?: boolean;
  maskTextSelector?: string | null;
  blockSelector?: string | null;
  ignoreClass?: string | RegExp;
  redactionPatterns?: Array<string | RegExp>;
};

export type RetraceBreadcrumbLevel = "debug" | "info" | "warning" | "error" | "fatal";

export type RetraceBreadcrumb = {
  /**
   * ms-since-epoch when the breadcrumb fired (defaults to `Date.now()`
   * when `addBreadcrumb` is called).
   */
  timestamp: number;
  /** `ui.click`, `http`, `console`, `navigation`, ... — Sentry shape. */
  category: string;
  /** Short human-readable trail line. */
  message: string;
  level: RetraceBreadcrumbLevel;
  data?: Record<string, unknown>;
};

export type RetraceBrowserOptions = {
  apiKey: string;
  ingestUrl?: string;
  sampleRate?: number;
  batchSize?: number;
  flushIntervalMs?: number;
  autoStart?: boolean;
  distinctId?: string;
  metadata?: Record<string, unknown>;
  privacy?: RetracePrivacyOptions;
  /**
   * Ring-buffer cap for auto- + manually-added breadcrumbs. Default
   * 50 (Sentry caps at 100 — we tilt smaller because every exception
   * event copies the buffer).
   */
  maxBreadcrumbs?: number;
};

export type RetraceClient = {
  identify: (distinctId: string, metadata?: Record<string, unknown>) => void;
  start: () => void;
  stop: () => void;
  flush: (flushType?: "normal" | "final") => Promise<void>;
  /** Append a breadcrumb to the ring buffer (also used internally by
   *  the auto-capture hooks). Drops the oldest entry when full. */
  addBreadcrumb: (breadcrumb: Partial<RetraceBreadcrumb> & { message: string }) => void;
  /** Read the current breadcrumb trail. Returns a copy so callers
   *  can't accidentally mutate the live ring. */
  getBreadcrumbs: () => RetraceBreadcrumb[];
};

const DEFAULT_INGEST_URL = "http://127.0.0.1:8788/api/sdk/replay";
const DEFAULT_MAX_BREADCRUMBS = 50;
const DEFAULT_REDACTION_PATTERNS = [
  /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi,
  /\b(?:token|secret|password|api[_-]?key)=([^&\s]+)/gi,
  /\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*/gi,
];

function makeSessionId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && "randomUUID" in cryptoObj) {
    return cryptoObj.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function makeHexId(bytes: number): string {
  const cryptoObj = globalThis.crypto;
  const buf = new Uint8Array(bytes);
  if (cryptoObj?.getRandomValues) {
    cryptoObj.getRandomValues(buf);
  } else {
    for (let i = 0; i < bytes; i += 1) {
      buf[i] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(buf)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function makeTraceparent(traceId?: string): string {
  const tid = traceId && /^[a-f0-9]{32}$/i.test(traceId) ? traceId : makeHexId(16);
  return `00-${tid.toLowerCase()}-${makeHexId(8)}-01`;
}

function traceIdFromTraceparent(traceparent: string | null | undefined): string {
  const match = String(traceparent || "").match(/^00-([a-f0-9]{32})-[a-f0-9]{16}-[a-f0-9]{2}$/i);
  return match ? match[1].toLowerCase() : "";
}

function shouldSample(sampleRate: number): boolean {
  if (sampleRate >= 1) return true;
  if (sampleRate <= 0) return false;
  return Math.random() < sampleRate;
}

function safeSerializeConsoleArg(item: unknown): string {
  if (typeof item === "string") return item;
  if (item instanceof Error) return `${item.name}: ${item.message}`;
  if (item instanceof Element) return `<${item.tagName.toLowerCase()}>`;
  try {
    return JSON.stringify(item);
  } catch {
    return Object.prototype.toString.call(item);
  }
}

function stackFromError(error: unknown): string {
  if (error instanceof Error) return error.stack || "";
  if (typeof error === "object" && error !== null && "stack" in error) {
    const stack = (error as { stack?: unknown }).stack;
    return typeof stack === "string" ? stack : "";
  }
  return "";
}

function messageFromError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null && "message" in error) {
    const message = (error as { message?: unknown }).message;
    return typeof message === "string" ? message : String(message ?? "");
  }
  return String(error ?? "");
}

function ensureGlobalPattern(pattern: RegExp): RegExp {
  const flags = pattern.flags.includes("g") ? pattern.flags : `${pattern.flags}g`;
  return new RegExp(pattern.source, flags);
}

export function init(options: RetraceBrowserOptions): RetraceClient {
  if (!options.apiKey) {
    throw new Error("[retrace] apiKey is required");
  }

  const ingestUrl = options.ingestUrl || DEFAULT_INGEST_URL;
  const batchSize = options.batchSize ?? 50;
  const flushIntervalMs = options.flushIntervalMs ?? 5000;
  const sessionId = makeSessionId();
  const enabled = shouldSample(options.sampleRate ?? 1);

  let distinctId = options.distinctId || "";
  let metadata = { ...(options.metadata || {}) };
  let sequence = 0;
  let activeTraceparent = "";
  let events: ReplayEvent[] = [];
  let stopRecording: (() => void) | undefined;
  let flushTimer: ReturnType<typeof setInterval> | undefined;
  let flushInFlight: Promise<void> | undefined;
  const cleanupFns: Array<() => void> = [];

  // Breadcrumb ring buffer. Bounded by `maxBreadcrumbs`; oldest is
  // dropped on overflow. We attach a copy to every "exception" event
  // so the server-side `monitoring_ingest` can promote them to
  // `IncidentEvidence.console_excerpts` / `network_failures`.
  const maxBreadcrumbs = Math.max(
    1,
    Math.min(500, options.maxBreadcrumbs ?? DEFAULT_MAX_BREADCRUMBS),
  );
  const breadcrumbs: RetraceBreadcrumb[] = [];

  // Best-effort deep clone — `structuredClone` is available in all
  // modern browsers and Node 17+. JSON fallback covers anything that
  // refuses the structured clone algorithm (functions, DOM nodes).
  // This stops nested-object mutation after capture from rewriting
  // historical breadcrumbs. (CodeRabbit Major catch on PR #130.)
  function cloneBreadcrumbData(
    data: Record<string, unknown> | undefined,
  ): Record<string, unknown> | undefined {
    if (!data) return undefined;
    const sc = (globalThis as { structuredClone?: (v: unknown) => unknown })
      .structuredClone;
    if (typeof sc === "function") {
      try {
        return sc(data) as Record<string, unknown>;
      } catch {
        /* fall through to JSON */
      }
    }
    try {
      return JSON.parse(JSON.stringify(data)) as Record<string, unknown>;
    } catch {
      // Last-resort shallow copy — rare (circular refs that bypass
      // structuredClone). Still better than handing out a live ref.
      return { ...data };
    }
  }

  // Strip query/fragment and credentials from URLs that end up in
  // breadcrumb data. Query strings carry tokens/emails/PII far more
  // often than we'd like; the path+origin is the useful signal.
  // (CodeRabbit Major catch on PR #130.)
  function sanitizeBreadcrumbUrl(raw: string): string {
    if (!raw) return raw;
    try {
      // Allow relative URLs by using a dummy base.
      const u = new URL(raw, "http://_b/");
      u.search = "";
      u.hash = "";
      u.username = "";
      u.password = "";
      if (u.origin === "http://_b") return u.pathname;
      return u.origin + u.pathname;
    } catch {
      // Not a URL we can parse — coarse strip.
      return raw.split("?")[0].split("#")[0];
    }
  }

  // Filter out our own ingest traffic so the ring doesn't fill with
  // self-noise on busy pages. (CodeRabbit Major catch on PR #130.)
  function isSdkIngestUrl(raw: string): boolean {
    if (!raw) return false;
    try {
      const probe = new URL(raw, "http://_b/");
      const target = new URL(ingestUrl, "http://_b/");
      return probe.origin === target.origin && probe.pathname === target.pathname;
    } catch {
      return raw === ingestUrl;
    }
  }

  function addBreadcrumbInternal(
    raw: Partial<RetraceBreadcrumb> & { message: string },
  ): void {
    const message = String(raw.message || "").slice(0, 500);
    if (!message) return;
    breadcrumbs.push({
      timestamp: typeof raw.timestamp === "number" ? raw.timestamp : Date.now(),
      category: String(raw.category || "default").slice(0, 80),
      message,
      level: (raw.level || "info") as RetraceBreadcrumbLevel,
      data: cloneBreadcrumbData(raw.data),
    });
    while (breadcrumbs.length > maxBreadcrumbs) {
      breadcrumbs.shift();
    }
  }

  function snapshotBreadcrumbs(): RetraceBreadcrumb[] {
    // Independent deep copies of the ring so the exception event
    // payload doesn't share mutable state with future captures.
    return breadcrumbs.map((b) => ({
      ...b,
      data: cloneBreadcrumbData(b.data),
    }));
  }
  const compiledRedactionPatterns = (() => {
    const custom = options.privacy?.redactionPatterns || [];
    const compiled = custom.flatMap((pattern) => {
      if (pattern instanceof RegExp) return [ensureGlobalPattern(pattern)];
      try {
        return [new RegExp(pattern, "gi")];
      } catch {
        return [];
      }
    });
    return DEFAULT_REDACTION_PATTERNS.concat(compiled);
  })();

  function redactText(value: string): string {
    return compiledRedactionPatterns.reduce(
      (text, pattern) => text.replace(pattern, "[redacted]"),
      value,
    );
  }

  function traceContext(): Record<string, unknown> {
    const globals = globalThis as unknown as {
      __RETRACE_TRACE_ID__?: string;
      __RETRACE_TRACEPARENT__?: string;
    };
    return {
      traceId: globals.__RETRACE_TRACE_ID__ || undefined,
      traceparent: globals.__RETRACE_TRACEPARENT__ || activeTraceparent || undefined,
    };
  }

  function ensureTraceparent(): string {
    const globals = globalThis as unknown as {
      __RETRACE_TRACE_ID__?: string;
      __RETRACE_TRACEPARENT__?: string;
    };
    const existing = globals.__RETRACE_TRACEPARENT__ || activeTraceparent;
    if (existing) return existing;
    activeTraceparent = makeTraceparent(globals.__RETRACE_TRACE_ID__);
    globals.__RETRACE_TRACEPARENT__ = activeTraceparent;
    globals.__RETRACE_TRACE_ID__ = traceIdFromTraceparent(activeTraceparent);
    return activeTraceparent;
  }

  function rememberTraceparent(traceparent: string | null): void {
    if (!traceparent) return;
    const traceId = traceIdFromTraceparent(traceparent);
    if (!traceId) return;
    const globals = globalThis as unknown as {
      __RETRACE_TRACE_ID__?: string;
      __RETRACE_TRACEPARENT__?: string;
    };
    activeTraceparent = traceparent;
    globals.__RETRACE_TRACEPARENT__ = traceparent;
    globals.__RETRACE_TRACE_ID__ = traceId;
  }

  function tracePayload(requestTraceparent: string, responseTraceparent?: string | null, fallbackTraceId?: string | null): Record<string, unknown> {
    return {
      traceparent: requestTraceparent,
      requestTraceparent,
      responseTraceparent: responseTraceparent || undefined,
      traceId:
        traceIdFromTraceparent(responseTraceparent) ||
        String(fallbackTraceId || "") ||
        traceIdFromTraceparent(requestTraceparent) ||
        undefined,
    };
  }

  function emitCustom(source: string, payload: Record<string, unknown>): void {
    events.push({
      type: 6,
      timestamp: Date.now(),
      data: {
        plugin: `retrace/${source}@1`,
        payload,
      },
    });
    if (events.length >= batchSize) {
      void flush();
    }
  }

  function selectorMatches(target: Element, selector: string | null | undefined): boolean {
    if (!selector) return false;
    try {
      return target.closest(selector) !== null;
    } catch {
      return false;
    }
  }

  function shouldCaptureTargetText(target: Element): boolean {
    if (
      options.privacy?.maskAllInputs !== false &&
      (target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement)
    ) {
      return false;
    }
    if (selectorMatches(target, options.privacy?.maskTextSelector)) return false;
    if (selectorMatches(target, options.privacy?.blockSelector)) return false;
    return true;
  }

  function labelTextFor(target: Element): string | undefined {
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) {
      const labels = Array.from(target.labels || []);
      const label = labels.find((item) => item.textContent?.trim());
      if (label) return label.textContent?.trim().slice(0, 120) || undefined;
    }
    const wrapped = target.closest("label");
    return wrapped?.textContent?.trim().slice(0, 120) || undefined;
  }

  function describeTarget(target: EventTarget | null): Record<string, unknown> {
    if (!(target instanceof Element)) {
      return {};
    }
    const testIdAttrName = ["data-testid", "data-test", "data-qa"].find((attr) =>
      target.getAttribute(attr),
    );
    const testIdValue = testIdAttrName
      ? target.getAttribute(testIdAttrName) || undefined
      : undefined;
    const description: Record<string, unknown> = {
      tagName: target.tagName.toLowerCase(),
      testIdAttrName,
      testIdValue,
      id: target.id || undefined,
      className: typeof target.className === "string" ? target.className : undefined,
      name: target.getAttribute("name") || undefined,
      role: target.getAttribute("role") || undefined,
      ariaLabel: target.getAttribute("aria-label") || undefined,
      accessibleName:
        target.getAttribute("aria-label") ||
        labelTextFor(target) ||
        (shouldCaptureTargetText(target) ? target.textContent?.trim().slice(0, 120) : undefined),
      labelText: labelTextFor(target),
      placeholder: target.getAttribute("placeholder") || undefined,
      title: target.getAttribute("title") || undefined,
    };
    if (shouldCaptureTargetText(target)) {
      description.text = target.textContent?.trim().slice(0, 120) || undefined;
    }
    return description;
  }

  function installInteractionCapture(): void {
    const onClick = (event: MouseEvent) => {
      const targetDesc = describeTarget(event.target);
      emitCustom("click", {
        x: event.clientX,
        y: event.clientY,
        button: event.button,
        target: targetDesc,
        url: globalThis.location?.href,
      });
      // Click breadcrumb — drop the visible text when the privacy
      // filter masks it, so we don't leak content from a masked
      // element via the breadcrumb trail. Both `text` AND the
      // ARIA-derived `accessibleName` fallback are gated on
      // `allowText`; otherwise an aria-label on a masked element
      // would still slip through. (CodeRabbit Major catch on PR #130.)
      const targetEl = event.target instanceof Element ? event.target : null;
      const allowText = targetEl ? shouldCaptureTargetText(targetEl) : false;
      const labelBits: string[] = [];
      if (typeof targetDesc.tagName === "string") labelBits.push(String(targetDesc.tagName));
      if (typeof targetDesc.id === "string" && targetDesc.id) labelBits.push(`#${targetDesc.id}`);
      if (allowText && typeof targetDesc.text === "string" && targetDesc.text) {
        labelBits.push(`"${String(targetDesc.text).slice(0, 80)}"`);
      } else if (allowText && typeof targetDesc.accessibleName === "string" && targetDesc.accessibleName) {
        labelBits.push(`"${String(targetDesc.accessibleName).slice(0, 80)}"`);
      }
      addBreadcrumbInternal({
        category: "ui.click",
        message: labelBits.join(" ") || "click",
        level: "info",
        data: {
          tagName: targetDesc.tagName,
          testIdValue: targetDesc.testIdValue,
          url: globalThis.location?.href,
        },
      });
    };
    const onInput = (event: Event) => {
      const target = event.target;
      emitCustom("input", {
        target: describeTarget(target),
        valueMasked: true,
        url: globalThis.location?.href,
      });
    };
    globalThis.document?.addEventListener("click", onClick, true);
    globalThis.document?.addEventListener("input", onInput, true);
    cleanupFns.push(() => {
      globalThis.document?.removeEventListener("click", onClick, true);
      globalThis.document?.removeEventListener("input", onInput, true);
    });
  }

  function installNavigationCapture(): void {
    if (typeof globalThis.history === "undefined") return;
    const lastUrlRef = { value: globalThis.location?.href || "" };
    const recordNav = (to: string, from: string, trigger: string) => {
      if (!to || to === from) return;
      addBreadcrumbInternal({
        category: "navigation",
        message: `${from || "—"} → ${to}`,
        level: "info",
        data: { from, to, trigger },
      });
    };
    const wrapHistoryMethod = (
      name: "pushState" | "replaceState",
    ): (() => void) => {
      const orig = globalThis.history[name] as History[typeof name];
      const wrapped = function patched(
        this: History,
        data: unknown,
        unused: string,
        url?: string | URL | null,
      ): void {
        const before = globalThis.location?.href || "";
        // History constructor types are strict on the 2nd arg; cast to
        // string for compatibility with all browsers.
        orig.call(this, data, unused, url ?? null);
        const after = globalThis.location?.href || "";
        recordNav(after, before, `history.${name}`);
        lastUrlRef.value = after;
      };
      (globalThis.history as History)[name] = wrapped as History[typeof name];
      return () => {
        (globalThis.history as History)[name] = orig;
      };
    };
    const restorePush = wrapHistoryMethod("pushState");
    const restoreReplace = wrapHistoryMethod("replaceState");
    const onPopState = () => {
      const before = lastUrlRef.value;
      const after = globalThis.location?.href || "";
      recordNav(after, before, "popstate");
      lastUrlRef.value = after;
    };
    const onHashChange = () => {
      const before = lastUrlRef.value;
      const after = globalThis.location?.href || "";
      recordNav(after, before, "hashchange");
      lastUrlRef.value = after;
    };
    globalThis.addEventListener?.("popstate", onPopState);
    globalThis.addEventListener?.("hashchange", onHashChange);
    cleanupFns.push(() => {
      restorePush();
      restoreReplace();
      globalThis.removeEventListener?.("popstate", onPopState);
      globalThis.removeEventListener?.("hashchange", onHashChange);
    });
  }

  function installConsoleCapture(): void {
    const originals: Partial<Record<"error" | "warn" | "info", (...data: unknown[]) => void>> = {};
    (["error", "warn", "info"] as const).forEach((level) => {
      originals[level] = globalThis.console[level].bind(globalThis.console);
      globalThis.console[level] = (...data: unknown[]) => {
        const serialized = data.map((item) => redactText(safeSerializeConsoleArg(item)));
        emitCustom("console", {
          level,
          payload: serialized,
          url: redactText(globalThis.location?.href || ""),
        });
        // Breadcrumb mirror — Sentry maps console levels to its own
        // breadcrumb levels (`error` ↔ `error`, `warn` ↔ `warning`).
        addBreadcrumbInternal({
          category: "console",
          message: serialized.join(" "),
          level: level === "warn" ? "warning" : level,
          data: { url: globalThis.location?.href },
        });
        originals[level]?.(...data);
      };
    });
    cleanupFns.push(() => {
      (["error", "warn", "info"] as const).forEach((level) => {
        if (originals[level]) {
          globalThis.console[level] = originals[level];
        }
      });
    });
  }

  function installErrorCapture(): void {
    const onError = (event: ErrorEvent) => {
      // Snapshot BEFORE we record the error-as-breadcrumb so the
      // exception payload's `breadcrumbs` are the trail leading up to
      // the error, not the error itself.
      const trail = snapshotBreadcrumbs();
      emitCustom("exception", {
        kind: "onerror",
        message: redactText(event.message || messageFromError(event.error)),
        stack: redactText(stackFromError(event.error)),
        source: redactText(event.filename || ""),
        line: event.lineno ?? undefined,
        column: event.colno ?? undefined,
        url: redactText(globalThis.location?.href || ""),
        sessionId,
        trace: traceContext(),
        breadcrumbs: trail,
      });
      addBreadcrumbInternal({
        category: "error",
        message: redactText(event.message || messageFromError(event.error)),
        level: "error",
        data: { source: event.filename, line: event.lineno },
      });
    };
    const onUnhandledRejection = (event: PromiseRejectionEvent) => {
      const trail = snapshotBreadcrumbs();
      emitCustom("exception", {
        kind: "unhandledrejection",
        message: redactText(messageFromError(event.reason)),
        stack: redactText(stackFromError(event.reason)),
        url: redactText(globalThis.location?.href || ""),
        sessionId,
        trace: traceContext(),
        breadcrumbs: trail,
      });
      addBreadcrumbInternal({
        category: "error",
        message: redactText(messageFromError(event.reason)),
        level: "error",
      });
    };
    globalThis.addEventListener?.("error", onError);
    globalThis.addEventListener?.("unhandledrejection", onUnhandledRejection);
    cleanupFns.push(() => {
      globalThis.removeEventListener?.("error", onError);
      globalThis.removeEventListener?.("unhandledrejection", onUnhandledRejection);
    });
  }

  function installNetworkCapture(): void {
    const originalFetch = globalThis.fetch?.bind(globalThis);
    if (originalFetch) {
      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const startedAt = Date.now();
        const requestTraceparent = ensureTraceparent();
        const headers = new Headers(
          init?.headers || (input instanceof Request ? input.headers : undefined),
        );
        if (!headers.has("traceparent")) {
          headers.set("traceparent", requestTraceparent);
        }
        const nextInit: RequestInit = { ...(init || {}), headers };
        const method =
          nextInit.method ||
          (input instanceof Request ? input.method : "GET");
        const url = input instanceof Request ? input.url : String(input);
        try {
          const response = await originalFetch(input, nextInit);
          const responseTraceparent = response.headers.get("traceparent");
          rememberTraceparent(responseTraceparent);
          const durationMs = Date.now() - startedAt;
          emitCustom("network", {
            kind: "fetch",
            method,
            url,
            status: response.status,
            durationMs,
            trace: tracePayload(
              requestTraceparent,
              responseTraceparent,
              response.headers.get("x-retrace-trace-id"),
            ),
          });
          // Skip our own ingest traffic from the breadcrumb trail —
          // otherwise the ring fills with self-noise on busy pages.
          if (!isSdkIngestUrl(url)) {
            const safeUrl = sanitizeBreadcrumbUrl(url);
            addBreadcrumbInternal({
              category: "http",
              message: `${method} ${safeUrl} → ${response.status}`,
              level: response.status >= 400 ? "error" : "info",
              data: { method, url: safeUrl, status_code: response.status, duration_ms: durationMs },
            });
          }
          return response;
        } catch (error) {
          const durationMs = Date.now() - startedAt;
          const errMsg = error instanceof Error ? error.message : String(error);
          emitCustom("network", {
            kind: "fetch",
            method,
            url,
            error: errMsg,
            durationMs,
            trace: tracePayload(requestTraceparent),
          });
          if (!isSdkIngestUrl(url)) {
            const safeUrl = sanitizeBreadcrumbUrl(url);
            addBreadcrumbInternal({
              category: "http",
              message: `${method} ${safeUrl} (failed: ${errMsg})`,
              level: "error",
              data: { method, url: safeUrl, error: errMsg, duration_ms: durationMs },
            });
          }
          throw error;
        }
      };
      cleanupFns.push(() => {
        globalThis.fetch = originalFetch;
      });
    }

    const OriginalXHR = globalThis.XMLHttpRequest;
    if (OriginalXHR) {
      const originalOpen = OriginalXHR.prototype.open;
      const originalSend = OriginalXHR.prototype.send;
      const originalSetRequestHeader = OriginalXHR.prototype.setRequestHeader;
      OriginalXHR.prototype.open = function open(
        this: XMLHttpRequest & { __retrace?: { method: string; url: string; traceparent: string; headers: Record<string, true> } },
        method: string,
        url: string | URL,
        async?: boolean,
        username?: string | null,
        password?: string | null,
      ) {
        this.__retrace = { method, url: String(url), traceparent: ensureTraceparent(), headers: {} };
        return originalOpen.call(this, method, url, async ?? true, username, password);
      };
      OriginalXHR.prototype.setRequestHeader = function setRequestHeader(
        this: XMLHttpRequest & { __retrace?: { method: string; url: string; traceparent: string; headers: Record<string, true> } },
        name: string,
        value: string,
      ) {
        if (this.__retrace) {
          this.__retrace.headers[String(name).toLowerCase()] = true;
        }
        return originalSetRequestHeader.call(this, name, value);
      };
      OriginalXHR.prototype.send = function send(
        this: XMLHttpRequest & { __retrace?: { method: string; url: string; traceparent: string; headers: Record<string, true> } },
        body?: Document | XMLHttpRequestBodyInit | null,
      ) {
        const startedAt = Date.now();
        const requestTraceparent = this.__retrace?.traceparent || ensureTraceparent();
        if (!this.__retrace?.headers.traceparent) {
          try {
            this.setRequestHeader("traceparent", requestTraceparent);
          } catch {
            // Some browser states reject header mutation; still record context.
          }
        }
        this.addEventListener("loadend", () => {
          const responseTraceparent = this.getResponseHeader("traceparent");
          rememberTraceparent(responseTraceparent);
          const xhrMethod = this.__retrace?.method || "GET";
          const xhrUrl = this.__retrace?.url || "";
          const durationMs = Date.now() - startedAt;
          emitCustom("network", {
            kind: "xhr",
            method: xhrMethod,
            url: xhrUrl,
            status: this.status,
            durationMs,
            trace: tracePayload(
              requestTraceparent,
              responseTraceparent,
              this.getResponseHeader("x-retrace-trace-id"),
            ),
          });
          if (!isSdkIngestUrl(xhrUrl)) {
            const safeUrl = sanitizeBreadcrumbUrl(xhrUrl);
            addBreadcrumbInternal({
              category: "http",
              message: `${xhrMethod} ${safeUrl} → ${this.status}`,
              level: this.status >= 400 ? "error" : "info",
              data: { method: xhrMethod, url: safeUrl, status_code: this.status, duration_ms: durationMs },
            });
          }
        });
        return originalSend.call(this, body);
      };
      cleanupFns.push(() => {
        OriginalXHR.prototype.open = originalOpen;
        OriginalXHR.prototype.send = originalSend;
        OriginalXHR.prototype.setRequestHeader = originalSetRequestHeader;
      });
    }
  }

  async function flush(flushType: "normal" | "final" = "normal"): Promise<void> {
    if (flushInFlight) {
      await flushInFlight;
      if (flushType === "final" && events.length > 0) {
        return flush(flushType);
      }
      return;
    }
    if (!enabled || events.length === 0) return;

    flushInFlight = (async () => {
      const sentEvents = events;
      const sentSequence = sequence;
      events = [];
      sequence += 1;
      const payload = {
        sessionId,
        sequence: sentSequence,
        flushType,
        distinctId,
        metadata: {
          ...metadata,
          url: globalThis.location?.href,
          userAgent: globalThis.navigator?.userAgent,
          viewport: {
            width: globalThis.innerWidth,
            height: globalThis.innerHeight,
          },
        },
        events: sentEvents,
      };
      try {
        const res = await fetch(ingestUrl, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "x-retrace-key": options.apiKey,
          },
          body: JSON.stringify(payload),
          keepalive: flushType === "final",
        });
        if (!res.ok) {
          events = sentEvents.concat(events);
          sequence = sentSequence;
        }
      } catch {
        events = sentEvents.concat(events);
        sequence = sentSequence;
      }
    })();

    try {
      await flushInFlight;
    } finally {
      flushInFlight = undefined;
    }
  }

  function start(): void {
    if (!enabled || stopRecording) return;
    installInteractionCapture();
    installNavigationCapture();
    installConsoleCapture();
    installErrorCapture();
    installNetworkCapture();
    stopRecording = record({
      emit(event) {
        events.push(event as ReplayEvent);
        if (events.length >= batchSize) {
          void flush();
        }
      },
      maskAllInputs: options.privacy?.maskAllInputs ?? true,
      maskTextSelector: options.privacy?.maskTextSelector,
      blockSelector: options.privacy?.blockSelector,
      ignoreClass: options.privacy?.ignoreClass,
    });
    flushTimer = setInterval(() => void flush(), flushIntervalMs);
  }

  function stop(): void {
    if (stopRecording) {
      stopRecording();
      stopRecording = undefined;
    }
    if (flushTimer) {
      clearInterval(flushTimer);
      flushTimer = undefined;
    }
    while (cleanupFns.length > 0) {
      cleanupFns.pop()?.();
    }
    void flush("final");
  }

  function identify(nextDistinctId: string, nextMetadata?: Record<string, unknown>): void {
    distinctId = nextDistinctId;
    metadata = { ...metadata, ...(nextMetadata || {}) };
  }

  const onPageHide = () => {
    void flush("final");
  };
  if (enabled) {
    globalThis.addEventListener?.("pagehide", onPageHide);
    cleanupFns.push(() => {
      globalThis.removeEventListener?.("pagehide", onPageHide);
    });
  }

  if (options.autoStart ?? true) {
    start();
  }

  function addBreadcrumb(
    breadcrumb: Partial<RetraceBreadcrumb> & { message: string },
  ): void {
    addBreadcrumbInternal(breadcrumb);
  }

  function getBreadcrumbs(): RetraceBreadcrumb[] {
    return snapshotBreadcrumbs();
  }

  return { identify, start, stop, flush, addBreadcrumb, getBreadcrumbs };
}
