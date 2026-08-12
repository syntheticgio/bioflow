interface BatchRenameFormProps {
  projectId: string;
  onBack: () => void;
}

export function BatchRenameForm({ onBack }: BatchRenameFormProps) {
  return (
    <div className="panel">
      <div className="panel-header">
        <button type="button" className="btn-text" onClick={onBack}>
          ← Back to project
        </button>
        <span className="panel-title">Batch rename files</span>
      </div>
      <div className="panel-body detail">
        <p>Rename multiple files in the project at once using pattern-based rules.</p>
        <p className="empty-title">Coming soon</p>
      </div>
    </div>
  );
}
