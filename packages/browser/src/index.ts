import { record } from "rrweb";

type ReplayEvent = Record<string, unknown>;

export type RetracePrivacyOptions = {
  maskAllInputs?: boolean;
  maskTextSelector?: string | null;
  blockSelector?: string | null;
  ignoreClass?: string | RegExp;
  redactionPatterns?: Array<string | RegExp>;
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
};

export type RetraceClient = {
  identify: (distinctId: string, metadata?: Record<string, unknown>) => void;
  start: () => void;
  stop: () => void;
  flush: (flushType?: "normal" | "final") => Promise<void>;
};

const DEFAULT_INGEST_URL = "http://127.0.0.1:8788/api/sdk/replay";
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
      emitCustom("click", {
        x: event.clientX,
        y: event.clientY,
        button: event.button,
        target: describeTarget(event.target),
        url: globalThis.location?.href,
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

  function installConsoleCapture(): void {
    const originals: Partial<Record<"error" | "warn" | "info", (...data: unknown[]) => void>> = {};
    (["error", "warn", "info"] as const).forEach((level) => {
      originals[level] = globalThis.console[level].bind(globalThis.console);
      globalThis.console[level] = (...data: unknown[]) => {
        emitCustom("console", {
          level,
          payload: data.map((item) => redactText(safeSerializeConsoleArg(item))),
          url: redactText(globalThis.location?.href || ""),
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
      });
    };
    const onUnhandledRejection = (event: PromiseRejectionEvent) => {
      emitCustom("exception", {
        kind: "unhandledrejection",
        message: redactText(messageFromError(event.reason)),
        stack: redactText(stackFromError(event.reason)),
        url: redactText(globalThis.location?.href || ""),
        sessionId,
        trace: traceContext(),
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
          emitCustom("network", {
            kind: "fetch",
            method,
            url,
            status: response.status,
            durationMs: Date.now() - startedAt,
            trace: tracePayload(
              requestTraceparent,
              responseTraceparent,
              response.headers.get("x-retrace-trace-id"),
            ),
          });
          return response;
        } catch (error) {
          emitCustom("network", {
            kind: "fetch",
            method,
            url,
            error: error instanceof Error ? error.message : String(error),
            durationMs: Date.now() - startedAt,
            trace: tracePayload(requestTraceparent),
          });
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
      OriginalXHR.prototype.open = function open(
        this: XMLHttpRequest & { __retrace?: { method: string; url: string; traceparent: string } },
        method: string,
        url: string | URL,
        async?: boolean,
        username?: string | null,
        password?: string | null,
      ) {
        this.__retrace = { method, url: String(url), traceparent: ensureTraceparent() };
        return originalOpen.call(this, method, url, async ?? true, username, password);
      };
      OriginalXHR.prototype.send = function send(
        this: XMLHttpRequest & { __retrace?: { method: string; url: string; traceparent: string } },
        body?: Document | XMLHttpRequestBodyInit | null,
      ) {
        const startedAt = Date.now();
        const requestTraceparent = this.__retrace?.traceparent || ensureTraceparent();
        try {
          this.setRequestHeader("traceparent", requestTraceparent);
        } catch {
          // Some browser states reject late header mutation; still record context.
        }
        this.addEventListener("loadend", () => {
          const responseTraceparent = this.getResponseHeader("traceparent");
          rememberTraceparent(responseTraceparent);
          emitCustom("network", {
            kind: "xhr",
            method: this.__retrace?.method || "GET",
            url: this.__retrace?.url || "",
            status: this.status,
            durationMs: Date.now() - startedAt,
            trace: tracePayload(
              requestTraceparent,
              responseTraceparent,
              this.getResponseHeader("x-retrace-trace-id"),
            ),
          });
        });
        return originalSend.call(this, body);
      };
      cleanupFns.push(() => {
        OriginalXHR.prototype.open = originalOpen;
        OriginalXHR.prototype.send = originalSend;
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

  return { identify, start, stop, flush };
}
