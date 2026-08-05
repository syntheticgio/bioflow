import { useState } from "react";
import { applySettings } from "./commands";
import type { Settings as SettingsValues } from "./types";

interface Props {
  current: SettingsValues;
  onClose: () => void;
  onApplied: (settings: SettingsValues) => void;
}

export function Settings({ current, onClose, onApplied }: Props) {
  const [storageLocation, setStorageLocation] = useState(current.storageLocation);
  const [port, setPort] = useState(current.port);
  const [networkExposed, setNetworkExposed] = useState(current.networkExposed);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);

  const storageChanged = storageLocation !== current.storageLocation;

  async function handleApply() {
    setApplying(true);
    setError(null);
    try {
      await applySettings({ storageLocation, port, networkExposed });
      onApplied({ storageLocation, port, networkExposed });
    } catch (e) {
      setError(String(e));
    } finally {
      setApplying(false);
    }
  }

  return (
    <section aria-label="Settings">
      <h2>Settings</h2>

      <label>
        Storage location
        <input
          value={storageLocation}
          onChange={(e) => setStorageLocation(e.target.value)}
        />
      </label>
      {storageChanged && (
        <p role="note">
          Changing the storage location points BioFlow at a different folder.
          Existing data does not move -- copy it yourself first if you want it
          to carry over.
        </p>
      )}

      <label>
        Port
        <input
          type="number"
          value={port}
          onChange={(e) => setPort(Number(e.target.value))}
        />
      </label>

      <label>
        <input
          type="checkbox"
          checked={networkExposed}
          onChange={(e) => setNetworkExposed(e.target.checked)}
        />
        Allow access from other devices on my network
      </label>
      {networkExposed && (
        <p role="note">
          Anyone on your network can reach BioFlow with no login required.
        </p>
      )}

      {error && <p role="alert">{error}</p>}

      <button onClick={handleApply} disabled={applying}>
        {applying ? "Applying…" : "Apply"}
      </button>
      <button onClick={onClose} disabled={applying}>
        Cancel
      </button>
    </section>
  );
}
