/**
 * Reserves a spot in the Help menu for a page that doesn't have content yet.
 * Swap this component out (and rename the route/menu label) once it does.
 */
export function HelpPlaceholder() {
  return (
    <div className="help-page">
      <h1>Coming soon</h1>
      <p className="help-intro">This page doesn't have content yet.</p>
    </div>
  );
}
