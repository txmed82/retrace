import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";

import { init, type RetraceBrowserOptions, type RetraceClient } from "./index";

const RetraceContext = createContext<RetraceClient | null>(null);

export function RetraceProvider(props: {
  children: ReactNode;
  options: RetraceBrowserOptions;
}) {
  const client = useMemo(() => init({ ...props.options, autoStart: false }), [props.options]);

  useEffect(() => {
    if (props.options.autoStart ?? true) {
      client.start();
    }
    return () => client.stop();
  }, [client, props.options.autoStart]);

  return createElement(RetraceContext.Provider, { value: client }, props.children);
}

export function useRetrace(): RetraceClient {
  const client = useContext<RetraceClient | null>(RetraceContext);
  if (!client) {
    throw new Error("useRetrace must be used inside RetraceProvider");
  }
  return client;
}
