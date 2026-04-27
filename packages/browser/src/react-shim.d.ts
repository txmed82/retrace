declare module "react" {
  export type ReactNode = unknown;

  export function createContext<T>(defaultValue: T): {
    Provider: unknown;
  };

  export function createElement(
    type: unknown,
    props: Record<string, unknown> | null,
    ...children: unknown[]
  ): unknown;

  export function useContext<T>(context: { Provider: unknown }): T;

  export function useEffect(
    effect: () => void | (() => void),
    deps?: unknown[],
  ): void;

  export function useMemo<T>(factory: () => T, deps?: unknown[]): T;

  export function useRef<T>(initialValue: T): {
    current: T;
  };
}
