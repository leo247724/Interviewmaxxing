import type { ApplicationView } from "@/lib/service/types";
import { RAIL_STEPS, isActive, railIndex, railStatuses } from "@/lib/state";
import { isPrepared } from "@/lib/preparation";

export function ProgressRail({ view }: { view: ApplicationView }) {
  const reached = railIndex(view);
  const statuses = railStatuses(view);
  const working = isActive(view.state);
  const prepared = isPrepared(view);
  const stopped = !working && view.state !== "SUBMITTED";
  const current = RAIL_STEPS[Math.max(reached, 0)];

  return (
    <div className={`rail${prepared ? " rail--prepared" : ""}`}>
      <p className="rail__compact">
        Step {Math.max(reached, 0) + 1} of {RAIL_STEPS.length} · {current.label}
        {prepared ? " · done, stopped before submitting" : stopped && " · paused"}
      </p>
      <ol className="rail__steps" aria-label="Application progress">
        {RAIL_STEPS.map((step, index) => {
          const status = statuses[index];
          return (
            <li
              key={step.key}
              className={`rail__step is-${status}`}
              aria-current={index === reached ? "step" : undefined}
            >
              <span className="rail__node" aria-hidden="true" />
              <span className="rail__label">
                {step.label}
                <span className="visually-hidden">
                  {status === "done"
                    ? " (done)"
                    : status === "current"
                      ? " (in progress)"
                      : status === "held"
                        ? " (stopped here)"
                        : prepared && index === reached + 1
                          ? " (not started: nothing was submitted)"
                          : ""}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
