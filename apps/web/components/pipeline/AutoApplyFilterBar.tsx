"use client";

import { useId } from "react";
import {
  filtersActive,
  toggleValue,
  type AutoApplyFilters,
  type FilterOption,
} from "@/lib/pipeline/filters";

type GroupKey = keyof AutoApplyFilters;

/**
 * Toggle chips that narrow the board by apply backend and by autonomous-apply
 * status and bottleneck. Multi-select within a group; groups combine with AND.
 */
export function AutoApplyFilterBar({
  options,
  filters,
  onChange,
  onClear,
  shown,
  total,
  hidden = false,
  onToggleHidden,
}: {
  options: Record<GroupKey, FilterOption[]>;
  filters: AutoApplyFilters;
  onChange: (filters: AutoApplyFilters) => void;
  onClear: () => void;
  shown: number;
  total: number;
  /** The chip groups are collapsed; the count and Clear filters stay while chips are active. */
  hidden?: boolean;
  onToggleHidden?: () => void;
}) {
  const groupsId = useId();
  const backendId = useId();
  const applyId = useId();
  const active = filtersActive(filters);
  const toggle = (group: GroupKey, value: string) => onChange({ ...filters, [group]: toggleValue(filters[group], value) });

  // A stored choice whose chip no longer appears in the data stays visible, so it can be turned off.
  const withSelected = (group: GroupKey): FilterOption[] => {
    const present = new Set(options[group].map((option) => option.value));
    const missing = filters[group]
      .filter((value) => !present.has(value))
      .map((value) => ({ value, label: value, count: 0 }));
    return [...options[group], ...missing];
  };

  const chips = (group: GroupKey, label: string) =>
    withSelected(group).map((option) => {
      const pressed = filters[group].includes(option.value);
      return (
        <button
          key={option.value}
          type="button"
          className="filter-chip"
          aria-pressed={pressed}
          onClick={() => toggle(group, option.value)}
        >
          <span>{option.label}</span>
          <span className="filter-chip__count">{option.count}</span>
          <span className="visually-hidden"> {label}</span>
        </button>
      );
    });

  return (
    <section className={`pipeline-filters${hidden ? " is-collapsed" : ""}`} aria-label="Filter cards">
      {onToggleHidden && (
        <div className="pipeline-filters__head">
          <button
            type="button"
            className="text-button pipeline-filters__toggle"
            aria-expanded={!hidden}
            aria-controls={groupsId}
            onClick={onToggleHidden}
          >
            {hidden ? "Show filters" : "Hide filters"}
          </button>
        </div>
      )}
      <div id={groupsId} className="pipeline-filters__groups" hidden={hidden}>
        <div className="pipeline-filters__group" role="group" aria-labelledby={backendId}>
          <span id={backendId} className="pipeline-filters__label">Backend</span>
          <div className="pipeline-filters__chips">{chips("backends", "backend")}</div>
        </div>
        <div className="pipeline-filters__group" role="group" aria-labelledby={applyId}>
          <span id={applyId} className="pipeline-filters__label">Autonomous apply</span>
          <div className="pipeline-filters__rows">
            <div className="pipeline-filters__chips" role="group" aria-label="Apply status">
              {options.statuses.length || filters.statuses.length ? (
                chips("statuses", "apply status")
              ) : (
                <span className="pipeline-filters__none">No apply status reported yet</span>
              )}
            </div>
            {(options.bottlenecks.length > 0 || filters.bottlenecks.length > 0) && (
              <div className="pipeline-filters__chips" role="group" aria-label="Bottleneck">
                {chips("bottlenecks", "bottleneck")}
              </div>
            )}
          </div>
        </div>
      </div>
      {(!hidden || active) && (
        <div className="pipeline-filters__foot">
          <p className="pipeline-filters__count" role="status" aria-live="polite">
            Showing {shown} of {total} cards
          </p>
          <button type="button" className="text-button" onClick={onClear} disabled={!active}>
            Clear filters
          </button>
        </div>
      )}
    </section>
  );
}
