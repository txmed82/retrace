import { record } from "rrweb";

type ReplayEvent = Record<string, unknown>;

export type RetracePrivacyOptions = {
  maskAllInputs?: boolean;
  maskTextSelector?: string;
  blockSelector?: string;
  ignoreClass?: string;
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

function makeSessionId(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && "randomUUID" in cryptoObj) {
    return cryptoObj.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
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
  let events: ReplayEvent[] = [];
  let stopRecording: (() => void) | undefined;
  let flushTimer: ReturnType<typeof setInterval> | undefined;
  const cleanupFns: Array<() => void> = [];

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

  function describeTarget(target: EventTarget | null): Record<string, unknown> {
    if (!(target instanceof Element)) {
      return {};
    }
    return {
      tagName: target.tagName.toLowerCase(),
      id: target.id || undefined,
      className: typeof target.className === "string" ? target.className : undefined,
      name: target.getAttribute("name") || undefined,
      role: target.getAttribute("role") || undefined,
      ariaLabel: target.getAttribute("aria-label") || undefined,
      text: target.textContent?.trim().slice(0, 120) || undefined,
    };
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
          payload: data.map(safeSerializeConsoleArg),
          url: globalThis.location?.href,
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

  function installNetworkCapture(): void {
    const originalFetch = globalThis.fetch?.bind(globalThis);
    if (originalFetch) {
      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const startedAt = Date.now();
        const method =
          init?.method ||
          (input instanceof Request ? input.method : "GET");
        const url = input instanceof Request ? input.url : String(input);
        try {
          const response = await originalFetch(input, init);
          emitCustom("network", {
            kind: "fetch",
            method,
            url,
            status: response.status,
            durationMs: Date.now() - startedAt,
          });
          return response;
        } catch (error) {
          emitCustom("network", {
            kind: "fetch",
            method,
            url,
            error: error instanceof Error ? error.message : String(error),
            durationMs: Date.now() - startedAt,
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
        this: XMLHttpRequest & { __retrace?: { method: string; url: string } },
        method: string,
        url: string | URL,
        async?: boolean,
        username?: string | null,
        password?: string | null,
      ) {
        this.__retrace = { method, url: String(url) };
        return originalOpen.call(this, method, url, async ?? true, username, password);
      };
      OriginalXHR.prototype.send = function send(
        this: XMLHttpRequest & { __retrace?: { method: string; url: string } },
        body?: Document | XMLHttpRequestBodyInit | null,
      ) {
        const startedAt = Date.now();
        this.addEventListener("loadend", () => {
          emitCustom("network", {
            kind: "xhr",
            method: this.__retrace?.method || "GET",
            url: this.__retrace?.url || "",
            status: this.status,
            durationMs: Date.now() - startedAt,
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
    if (!enabled || events.length === 0) return;
    const payload = {
      sessionId,
      sequence,
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
      events,
    };
    const sentEvents = events;
    events = [];
    sequence += 1;
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
        sequence -= 1;
      }
    } catch {
      events = sentEvents.concat(events);
      sequence -= 1;
    }
  }

  function start(): void {
    if (!enabled || stopRecording) return;
    installInteractionCapture();
    installConsoleCapture();
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

  globalThis.addEventListener?.("pagehide", () => {
    void flush("final");
  });

  if (options.autoStart ?? true) {
    start();
  }

  return { identify, start, stop, flush };
}
