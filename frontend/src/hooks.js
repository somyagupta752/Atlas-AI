import { useCallback, useEffect, useRef, useState } from "react";

// Loads once, and again every `pollMs` while `shouldPoll(data)` says the data is still changing.
export function useLoad(loader, deps, { pollMs = 0, shouldPoll = () => false } = {}) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const alive = useRef(true);
  const load = useCallback(async (quiet) => {
    if (!quiet) setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await loader();
      if (alive.current) setState({ loading: false, error: null, data });
      return data;
    } catch (error) {
      if (alive.current) setState((s) => ({ loading: false, error: error.message, data: quiet ? s.data : null }));
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    alive.current = true;
    let timer;
    const tick = async (quiet) => {
      const data = await load(quiet);
      if (alive.current && pollMs && data && shouldPoll(data)) timer = setTimeout(() => tick(true), pollMs);
    };
    tick(false);
    return () => {
      alive.current = false;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, pollMs]);
  return { ...state, reload: () => load(false) };
}
