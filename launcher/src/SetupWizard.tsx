import { useEffect, useState } from "react";
import {
  runFirstSetup,
  setupDefaults,
  validateSetupPort,
  validateStorage,
  type PortValidation,
  type StoragePathValidation,
} from "./commands";

interface Props {
  onInstalled: (installed: { storageLocation: string; port: number }) => void;
}

const STORAGE_MESSAGES: Record<StoragePathValidation["kind"], string | null> = {
  Ok: null,
  DoesNotExist: "This folder does not exist.",
  NotWritable: "BioFlow cannot write to this folder.",
  NotDockerShared:
    "On macOS this folder must be shared with Docker Desktop (Settings > Resources > File Sharing), or BioFlow will start with no data visible.",
};

const PORT_MESSAGES: Record<PortValidation["kind"], string | null> = {
  Ok: null,
  InUse: "This port is already in use. Pick another.",
};

export function SetupWizard({ onInstalled }: Props) {
  const [loaded, setLoaded] = useState(false);
  const [storageLocation, setStorageLocation] = useState("");
  const [installDir, setInstallDir] = useState("");
  const [port, setPort] = useState(5173);

  const [storageValidation, setStorageValidation] = useState<StoragePathValidation>({
    kind: "Ok",
  });
  const [portValidation, setPortValidation] = useState<PortValidation>({ kind: "Ok" });

  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);

  useEffect(() => {
    setupDefaults().then((d) => {
      setStorageLocation(d.storageLocation);
      setInstallDir(d.installDir);
      setPort(d.port);
      setLoaded(true);
    });
  }, []);

  useEffect(() => {
    if (!storageLocation) return;
    const id = setTimeout(() => {
      validateStorage(storageLocation).then(setStorageValidation);
    }, 300);
    return () => clearTimeout(id);
  }, [storageLocation]);

  useEffect(() => {
    const id = setTimeout(() => {
      validateSetupPort(port).then(setPortValidation);
    }, 300);
    return () => clearTimeout(id);
  }, [port]);

  const canInstall =
    loaded &&
    storageLocation.length > 0 &&
    installDir.length > 0 &&
    storageValidation.kind === "Ok" &&
    portValidation.kind === "Ok";

  async function handleInstall() {
    setInstalling(true);
    setInstallError(null);
    try {
      await runFirstSetup({ storageLocation, installDir, port });
      onInstalled({ storageLocation, port });
    } catch (e) {
      setInstallError(String(e));
    } finally {
      setInstalling(false);
    }
  }

  if (!loaded) {
    return <p>Loading…</p>;
  }

  return (
    <section aria-label="First-run setup">
      <h2>Set up BioFlow</h2>

      <label>
        Storage location
        <input
          value={storageLocation}
          onChange={(e) => setStorageLocation(e.target.value)}
          disabled={installing}
        />
      </label>
      {STORAGE_MESSAGES[storageValidation.kind] && (
        <p role={storageValidation.kind === "NotDockerShared" ? "note" : "alert"}>
          {STORAGE_MESSAGES[storageValidation.kind]}
        </p>
      )}

      <label>
        Install directory
        <input
          value={installDir}
          onChange={(e) => setInstallDir(e.target.value)}
          disabled={installing}
        />
      </label>

      <label>
        Port
        <input
          type="number"
          value={port}
          onChange={(e) => setPort(Number(e.target.value))}
          disabled={installing}
        />
      </label>
      {PORT_MESSAGES[portValidation.kind] && <p role="alert">{PORT_MESSAGES[portValidation.kind]}</p>}

      {installError && (
        <pre role="alert" style={{ whiteSpace: "pre-wrap" }}>
          {installError}
        </pre>
      )}

      <button onClick={handleInstall} disabled={!canInstall || installing}>
        {installing ? "Setting up…" : "Install"}
      </button>
    </section>
  );
}
