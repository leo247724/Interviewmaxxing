import type { EvidenceView } from "@/lib/service/types";
import { formatClock } from "@/lib/format";

export function EvidenceList({ items, title = "Evidence" }: { items: EvidenceView[]; title?: string }) {
  if (items.length === 0) return null;
  return (
    <section className="evidence" aria-label={title}>
      <h3 className="evidence__title">{title}</h3>
      <ul className="evidence__list">
        {items.map((item, index) => (
          <li key={`${item.kind}-${index}`} className={`evidence__item evidence--${item.kind}`}>
            <p className="evidence__label">
              {item.label}
              <span className={`evidence__source source-${item.source}`}>
                {item.source === "site" ? "Observed on the site" : "Reported by you"} · {formatClock(item.observedAt)}
              </span>
            </p>
            {item.kind === "screenshot" && item.href ? (
              <a className="evidence__shot" href={item.href} target="_blank" rel="noreferrer noopener">
                {/* Plain <img>: artifacts come from the service with unknown dimensions. */}
                <img src={item.href} alt={item.label} loading="lazy" />
                <span className="visually-hidden"> (opens full size in a new tab)</span>
              </a>
            ) : item.kind === "page_text" || item.kind === "portal" || item.kind === "email" ? (
              item.value && <blockquote className="evidence__quote">{item.value}</blockquote>
            ) : item.kind === "page_url" ? (
              item.value && <p className="mono evidence__url">{item.value}</p>
            ) : (
              item.value && <p className="evidence__note">{item.value}</p>
            )}
            {item.href && item.kind !== "screenshot" && (
              <a className="text-link" href={item.href} target="_blank" rel="noreferrer noopener">
                Open
              </a>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
