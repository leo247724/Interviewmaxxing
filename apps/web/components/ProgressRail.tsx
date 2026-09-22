import type { ApplicationView } from "@/lib/service/types";
import { RAIL_STEPS, isActive, railIndex } from "@/lib/state";

export function ProgressRail({ view }: { view: ApplicationView }) {
  const reached = railIndex(view);
  const working = isActive(view.state);
  const stopped = !working && view.state !== "SUBMITTED";
  const current = RAIL_STEPS[Math.max(reached, 0)];

  return (
    <div className="rail">
      <p className="rail__compact">
        Step {Math.max(reached, 0) + 1} of {RAIL_STEPS.length} · {current.label}
        {stopped && " · paused"}
      </p>
      <ol className="rail__steps" aria-label="Application progress">
        {RAIL_STEPS.map((step, index) => {
          const status =
            index < reached || (index === reached && view.state === "SUBMITTED")
              ? "done"
              : index === reached
                ? stopped
                  ? "held"
                  : "current"
                : "todo";
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
