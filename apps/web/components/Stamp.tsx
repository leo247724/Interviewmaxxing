/** Postal-style state mark. Decorative: the headline carries the meaning. */
export function Stamp({
  tone,
  word,
  date,
}: {
  tone: "success" | "caution" | "failure" | "neutral";
  word: string;
  date: string;
}) {
  const parts = new Intl.DateTimeFormat("en-US", { day: "2-digit", month: "short", year: "numeric" }).formatToParts(
    new Date(date),
  );
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  const stamped = `${part("day")} ${part("month")} ${part("year")}`.toUpperCase();
  return (
    <span className={`stamp stamp--${tone}`} aria-hidden="true" data-testid="state-stamp">
      <span className="stamp__word">{word}</span>
      <span className="stamp__date">{stamped}</span>
    </span>
  );
}
