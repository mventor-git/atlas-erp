import { useCallback, useEffect, useRef, useState } from "react";

export type Resource<T> =
  | { status: "loading"; data: null; error: null; reload: () => void }
  | { status: "ready"; data: T; error: null; reload: () => void }
  | { status: "error"; data: null; error: string; reload: () => void };

/**
 * One GET, one parse, three states.
 *
 * Each panel on the console is a read of one API route, so they all need the
 * same three states and a retry; that is the whole abstraction. Nothing is
 * cached across routes and nothing is polled, because the data is in-memory
 * demo state that a refresh is meant to re-read.
 */
export function useResource<T>(load: () => Promise<T>): Resource<T> {
  const [resource, setResource] = useState<Resource<T>>({
    status: "loading",
    data: null,
    error: null,
    // Replaced by the first run below, in the mount effect. The loading state
    // renders no retry, so nothing can call this before it is replaced.
    reload: () => undefined,
  });
  // A route that unmounts mid-flight must not set state, and a retry must not
  // let an older request overwrite a newer one.
  const request = useRef(0);

  // Annotated because each state stores `run` as its own reload.
  const run: () => void = useCallback(() => {
    const current = ++request.current;
    setResource({ status: "loading", data: null, error: null, reload: run });
    load().then(
      (data) => {
        if (request.current === current) {
          setResource({ status: "ready", data, error: null, reload: run });
        }
      },
      (error: unknown) => {
        if (request.current !== current) return;
        setResource({
          status: "error",
          data: null,
          error: error instanceof Error ? error.message : "Unknown error.",
          reload: run,
        });
      },
    );
  }, [load]);

  useEffect(() => {
    run();
    return () => {
      request.current += 1;
    };
  }, [run]);

  return resource;
}
