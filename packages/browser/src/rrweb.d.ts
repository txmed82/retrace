declare module "rrweb" {
  export function record(options: {
    emit: (event: Record<string, unknown>) => void;
    maskAllInputs?: boolean;
    maskTextSelector?: string;
    blockSelector?: string;
    ignoreClass?: string;
  }): (() => void) | undefined;
}
