import { useCallback, useEffect, useState } from "react";

import type { Navigate, NavigateOptions } from "./AppLink";
import { pushHistoryEntry, replaceHistoryEntry } from "./navHistory";
import { resolveRoute, type ResolvedRoute } from "./routes";

function readCurrentRoute(): ResolvedRoute {
  return resolveRoute(window.location.pathname);
}

// `search` is the current query string, so pages that keep their view in the URL re-read it on every navigation.
export function useAppRouter(): Readonly<{ resolved: ResolvedRoute; search: string; navigate: Navigate }> {
  const [resolved, setResolved] = useState<ResolvedRoute>(() => readCurrentRoute());
  const [search, setSearch] = useState(() => window.location.search);

  const synchronize = useCallback((replaceCanonical = false) => {
    const next = readCurrentRoute();
    if (next.shouldReplace || replaceCanonical) {
      replaceHistoryEntry({}, next.canonicalPath);
    }
    setResolved({ ...next, shouldReplace: false });
    setSearch(window.location.search);
  }, []);

  useEffect(() => {
    synchronize();
    const onPopState = () => synchronize();
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [synchronize]);

  const navigate = useCallback<Navigate>((href: string, options?: NavigateOptions) => {
    const destination = new URL(href, window.location.href);
    if (destination.origin !== window.location.origin) {
      window.location.assign(destination.href);
      return;
    }

    const next = resolveRoute(destination.pathname);
    const target = `${next.canonicalPath}${destination.search}${destination.hash}`;
    if (options?.replace) {
      replaceHistoryEntry({}, target);
    } else if (target !== `${window.location.pathname}${window.location.search}${window.location.hash}`) {
      pushHistoryEntry({}, target);
    }
    setResolved({ ...next, shouldReplace: false });
    setSearch(destination.search);
  }, []);

  return { resolved, search, navigate };
}
