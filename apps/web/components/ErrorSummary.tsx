"use client";

import { useEffect, useRef } from "react";

export interface SummaryItem {
  fieldId: string;
  message: string;
}

/**
 * Lists validation problems with links to each field. Receives focus whenever
 * `attempt` changes so keyboard and screen-reader users land on it.
 */
export function ErrorSummary({ title, items, attempt }: { title: string; items: SummaryItem[]; attempt: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (items.length > 0) ref.current?.focus();
    // Focus once per attempt, not on every keystroke that changes `items`.
  }, [attempt]);

  if (items.length === 0) return null;

  return (
    <div className="error-summary" role="alert" tabIndex={-1} ref={ref}>
      <p className="error-summary__title">{title}</p>
      <ul>
        {items.map((item) => (
          <li key={item.fieldId}>
            <a
              href={`#${item.fieldId}`}
              onClick={(event) => {
                const target = document.getElementById(item.fieldId);
                if (target) {
                  event.preventDefault();
                  target.scrollIntoView({ block: "center" });
                  const focusable = target.matches("input, select, textarea, button")
                    ? target
                    : target.querySelector<HTMLElement>("input, select, textarea, button");
                  focusable?.focus({ preventScroll: true });
                }
              }}
            >
              {item.message}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
