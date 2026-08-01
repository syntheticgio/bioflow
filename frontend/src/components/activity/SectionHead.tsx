/**
 * A column heading and its rule.
 *
 * The rule is markup rather than a `border-bottom` on the heading because it
 * runs the full width of the column while the heading sits in a baseline row
 * beside its note -- the two cannot be the same box.
 */
export function SectionHead({ title, note }: { title: string; note?: string }) {
  return (
    <>
      <div className="activity-head">
        <h6 className="activity-head-title">{title}</h6>
        {note && <span className="activity-head-note">{note}</span>}
      </div>
      <div className="activity-rule" />
    </>
  );
}
