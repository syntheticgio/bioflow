interface ExportSummaryFormProps {
  projectId: string;
  onBack: () => void;
}

export function ExportSummaryForm({ onBack }: ExportSummaryFormProps) {
  return (
    <div className="panel">
      <div className="panel-header">
        <button type="button" className="btn-text" onClick={onBack}>
          ← Back to project
        </button>
        <span className="panel-title">Export project summary</span>
      </div>
      <div className="panel-body detail">
        <p>Export a summary of the project's files, metadata, and quality metrics.</p>
        <p className="empty-title">Coming soon</p>
      </div>
    </div>
  );
}
