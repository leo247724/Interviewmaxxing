"use client";

import { useCallback, useEffect, useState } from "react";
import { getReadiness, type ServiceReadiness } from "@/lib/service/readiness";

export function useReadiness(mode: "live" | "preview") {
  const [readiness, setReadiness] = useState<ServiceReadiness | null>(null);
  const [checking, setChecking] = useState(mode === "live");
  const refresh = useCallback(async () => {
    if (mode === "preview") return null;
    setChecking(true);
    try {
      const current = await getReadiness();
      setReadiness(current);
      return current;
    }
    catch { setReadiness(null); return null; }
    finally { setChecking(false); }
  }, [mode]);
  useEffect(() => { void refresh(); }, [refresh]);
  return { readiness, checking, refresh };
}
