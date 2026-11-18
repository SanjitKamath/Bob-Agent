import { useEffect, useState } from "react";
import type { ApiRequest, AppState, BobBridge } from "../../shared/types";

declare global {
  interface Window {
    bob: BobBridge;
  }
}

export const bob = window.bob;

/** The app state from the main process, kept current. */
export function useAppState(): AppState | null {
  const [state, setState] = useState<AppState | null>(null);
  useEffect(() => {
    let live = true;
    void bob.state().then((initial) => live && setState(initial));
    const stop = bob.onState(setState);
    return () => {
      live = false;
      stop();
    };
  }, []);
  return state;
}

/** Call bobd and return the data, or throw an Error with bobd's message. */
export async function call<T>(method: ApiRequest["method"], path: string, body?: unknown): Promise<T> {
  const result = await bob.api({ method, path, body });
  if (!result.ok) throw Object.assign(new Error(result.error), { status: result.status, data: result.data });
  return result.data as T;
}

/** Load something from bobd, with reload and error state. */
export function useLoad<T>(path: string | null, deps: unknown[] = []): { data: T | null; error: string; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) return;
    let live = true;
    call<T>("GET", path)
      .then((value) => {
        if (!live) return;
        setData(value);
        setError("");
      })
      .catch((problem: Error) => live && setError(problem.message));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tick, ...deps]);
  return { data, error, reload: () => setTick((value) => value + 1) };
}
