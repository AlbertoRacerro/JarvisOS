import { useEffect, useState } from "react";

const SERIES_COUNT = 8;

export function chartSeriesColor(index: number): string {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(`--color-chart-series-${index % SERIES_COUNT + 1}`).trim();
}

export function useChartAppearance(): string {
  const [appearance, setAppearance] = useState(() => document.documentElement.dataset.theme ?? "");
  useEffect(() => {
    const observer = new MutationObserver(() => setAppearance(document.documentElement.dataset.theme ?? ""));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return appearance;
}
