import { useId } from "react";
import type { ReviewAnswerView } from "@/lib/service/types";
import {
  confidenceLabel,
  groupReviewByPage,
  needsCheck,
  reviewDisplay,
  reviewSummary,
  sourceLabel,
} from "@/lib/preparation";

/** The answers the desk entered into the form, grouped by page, for the person to check. */
export function ReviewList({ items }: { items: ReviewAnswerView[] }) {
  const titleId = useId();
  const groups = groupReviewByPage(items);
  const { total, pages, toCheck } = reviewSummary(items);
  const paged = groups.length > 1;

  return (
    <section className="review" aria-labelledby={titleId}>
      <h3 id={titleId} className="review__title">
        What the desk entered
      </h3>
      {total === 0 ? (
        <p className="review__empty">
          The service didn&rsquo;t list the answers it entered. The screenshot shows the filled form.
        </p>
      ) : (
        <>
          <p className="review__summary">
            {total} {total === 1 ? "answer" : "answers"}
            {pages > 1 ? ` on ${pages} pages` : ""}
            {toCheck > 0 ? `. ${toCheck} flagged to check: the desk was less sure of ${toCheck === 1 ? "it" : "them"}.` : "."}
          </p>
          {groups.map((group) => (
            <ReviewGroup key={group.page ?? "unknown"} page={group.page} items={group.items} paged={paged} />
          ))}
        </>
      )}
    </section>
  );
}

function ReviewGroup({ page, items, paged }: { page: number | null; items: ReviewAnswerView[]; paged: boolean }) {
  const headingId = useId();
  return (
    <div className="review__group">
      {paged && (
        <h4 id={headingId} className="review__page">
          {page === null ? "Page not recorded" : `Page ${page}`}
        </h4>
      )}
      <dl className="review__list" aria-labelledby={paged ? headingId : undefined}>
        {items.map((item, index) => (
          <ReviewRow key={index} item={item} />
        ))}
      </dl>
    </div>
  );
}

function ReviewRow({ item }: { item: ReviewAnswerView }) {
  const check = needsCheck(item);
  const confidence = confidenceLabel(item.confidence);
  const display = reviewDisplay(item);

  return (
    <div className={`review__item${check ? " is-check" : ""}`}>
      <dt className="review__question">
        {item.question}
        {!item.wordingRecorded && (
          <span className="review__unrecorded">Wording not recorded, see the screenshot</span>
        )}
      </dt>
      <dd className="review__answer">
        {display.kind === "blank" ? (
          <span className="review__blank">Left blank</span>
        ) : display.kind === "list" ? (
          <ul className="review__choices">
            {display.items.map((choice, index) => (
              <li key={index}>{choice}</li>
            ))}
          </ul>
        ) : display.kind === "long_text" ? (
          <p className="review__long">{display.text}</p>
        ) : display.kind === "file" ? (
          <span className="review__file mono">{display.name}</span>
        ) : (
          display.text
        )}
      </dd>
      <dd className="review__meta">
        <span className={`review__source review__source--${item.source}`}>{sourceLabel(item.source)}</span>
        {confidence && <span className="review__confidence">{confidence}</span>}
        {check && <span className="review__flag">Check this</span>}
      </dd>
    </div>
  );
}
