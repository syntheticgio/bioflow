import { useNavigate } from "react-router-dom";
import { BatchRenameForm } from "./BatchRenameForm";
import { BatchTagForm } from "./BatchTagForm";
import { ExportSummaryForm } from "./ExportSummaryForm";
import { MergeFastqForm } from "./MergeFastqForm";
import { QcAllReadsForm } from "./QcAllReadsForm";

interface OperationPanelProps {
  name: string;
  projectId: string;
}

export function OperationPanel({ name, projectId }: OperationPanelProps) {
  const navigate = useNavigate();

  const backToProject = () => {
    const params = new URLSearchParams(window.location.search);
    params.delete("sel");
    navigate(`${window.location.pathname}?${params}`, { replace: true });
  };

  switch (name) {
    case "merge_fastq":
      return <MergeFastqForm projectId={projectId} onBack={backToProject} />;
    case "batch_rename":
      return <BatchRenameForm projectId={projectId} onBack={backToProject} />;
    case "batch_tags":
      return <BatchTagForm projectId={projectId} onBack={backToProject} />;
    case "export":
      return <ExportSummaryForm projectId={projectId} onBack={backToProject} />;
    case "qc_all":
      return <QcAllReadsForm projectId={projectId} onBack={backToProject} />;
    default:
      return (
        <div className="panel">
          <div className="panel-body">
            <div className="empty">
              <div className="empty-title">Unknown operation</div>
              <p>Operation &ldquo;{name}&rdquo; is not recognised.</p>
              <button type="button" className="btn-text" onClick={backToProject}>
                ← Back to project
              </button>
            </div>
          </div>
        </div>
      );
  }
}
