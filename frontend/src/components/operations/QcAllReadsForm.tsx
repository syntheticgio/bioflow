interface QcAllReadsFormProps {
  projectId: string;
  onBack: () => void;
}

export function QcAllReadsForm({ onBack }: QcAllReadsFormProps) {
  return (
    <div className="panel">
      <div className="panel-header">
        <button type="button" className="btn-text" onClick={onBack}>
          ← Back to project
        </button>
        <span className="panel-title">Run QC on all reads</span>
      </div>
      <div className="panel-body detail">
        <p>Run quality control checks on every FASTQ file in the project.</p>
        <p className="empty-title">Coming soon</p>
      </div>
    </div>
  );
}
