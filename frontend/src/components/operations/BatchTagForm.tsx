interface BatchTagFormProps {
  projectId: string;
  onBack: () => void;
}

export function BatchTagForm({ onBack }: BatchTagFormProps) {
  return (
    <div className="panel">
      <div className="panel-header">
        <button type="button" className="btn-text" onClick={onBack}>
          ← Back to project
        </button>
        <span className="panel-title">Batch tag/metadata</span>
      </div>
      <div className="panel-body detail">
        <p>Add or edit metadata tags across multiple files in the project at once.</p>
        <p className="empty-title">Coming soon</p>
      </div>
    </div>
  );
}
