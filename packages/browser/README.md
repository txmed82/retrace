# @retrace/browser

Browser replay capture SDK for Retrace first-party ingest.

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
