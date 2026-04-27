declare module "rrweb" {
  export function record(options: {
    emit: (event: Record<string, unknown>, isCheckout?: boolean) => void;
    blockClass?: string | RegExp;
    blockSelector?: string | null;
    maskAllInputs?: boolean;
    maskTextSelector?: string | null;
    ignoreClass?: string | RegExp;
    [key: string]: unknown;
  }): () => void;
}
