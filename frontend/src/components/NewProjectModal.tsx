import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { notify } from "../stores/messageStore";

interface Props {
  parentId?: string;
  onClose: () => void;
}

export function NewProjectModal({ parentId, onClose }: Props) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () =>
      api.createProject({ name: name.trim(), description, parent_id: parentId }),
    onSuccess: (project) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      notify.success(`Created project “${project.name}”`);
      onClose();
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (name.trim()) mutation.mutate();
  };

  return (
    <div
      className="modal-backdrop"
      onClick={onClose}
      onKeyDown={(e) => e.key === "Escape" && onClose()}
    >
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>New project</h2>
        <form onSubmit={submit}>
          <label htmlFor="np-name">Name</label>
          <input
            id="np-name"
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Patient Cohort A"
          />

          <label htmlFor="np-desc">Description</label>
          <textarea
            id="np-desc"
            rows={3}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Optional"
          />

          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn primary"
              disabled={!name.trim() || mutation.isPending}
            >
              {mutation.isPending ? "Creating…" : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
