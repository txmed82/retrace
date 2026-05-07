# @retrace/browser

Browser replay capture SDK for Retrace first-party ingest.

## Install

```bash
npm install @retrace/browser
```

Create a write-only public SDK key from your Retrace server:

```bash
retrace api create-sdk-key --project Web --environment production
retrace api serve
```

```ts
import { init } from "@retrace/browser";

const retrace = init({
  apiKey: "rtpk_...",
  ingestUrl: "https://retrace.example.com/api/sdk/replay",
  distinctId: user.id,
  metadata: { plan: user.plan },
  privacy: {
    maskAllInputs: true,
    blockSelector: "[data-retrace-block]",
    maskTextSelector: "[data-retrace-mask]",
  },
});

retrace.identify(user.id, { email: user.email });
```

React apps can use the separate React entrypoint:

```tsx
import { RetraceProvider } from "@retrace/browser/react";

export function AppRoot({ children }) {
  return (
    <RetraceProvider options={{ apiKey: "rtpk_..." }}>
      {children}
    </RetraceProvider>
  );
}
```

The SDK records rrweb replay events and adds Retrace plugin events for clicks,
inputs, console messages, fetch calls, and XHR calls. Batches are sent with a
write-only public SDK key to `POST /api/sdk/replay`.

## What Retrace Captures

Retrace uses rrweb for replay data, then adds product-specific events that make
failures easier to convert into tests:

- Click and input interactions with target metadata.
- Console errors and warnings.
- `fetch` and XHR request failures.
- Session, user, and application metadata supplied during `init()`.

For replay-derived UI tests, the SDK captures durable target fields when
available: `data-testid`, `data-test`, `data-qa`, role, name, aria label, id,
and safe text. Retrace prefers those fields when generating regression selectors.

## Privacy Defaults

Use `maskAllInputs`, `blockSelector`, and `maskTextSelector` for production
apps. Public SDK keys can only write replay batches; read access uses service
tokens created separately by the Retrace API.
