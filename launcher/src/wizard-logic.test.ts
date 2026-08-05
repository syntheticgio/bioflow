import { describe, expect, it } from "vitest";
import { canInstall, setupStatusText, storageLocationChanged } from "./wizard-logic";

const OK_STORAGE = { kind: "Ok" as const };
const OK_PORT = { kind: "Ok" as const };
const BAD_STORAGE = { kind: "NotWritable" as const };
const BAD_PORT = { kind: "InUse" as const };

describe("canInstall", () => {
  it("allows install once loaded with a storage location and no validation problems", () => {
    expect(
      canInstall({
        loaded: true,
        storageLocation: "/home/user/BioFlow",
        storageValidation: OK_STORAGE,
        portValidation: OK_PORT,
      }),
    ).toBe(true);
  });

  it("blocks install before setup_defaults has loaded, even with valid-looking fields", () => {
    // The real bug this guards: a race where the wizard renders with its
    // initial empty-string/port-5173 state before setupDefaults() resolves
    // must not let Install fire against stale defaults.
    expect(
      canInstall({
        loaded: false,
        storageLocation: "/home/user/BioFlow",
        storageValidation: OK_STORAGE,
        portValidation: OK_PORT,
      }),
    ).toBe(false);
  });

  it("blocks install when the storage location is empty", () => {
    expect(
      canInstall({
        loaded: true,
        storageLocation: "",
        storageValidation: OK_STORAGE,
        portValidation: OK_PORT,
      }),
    ).toBe(false);
  });

  it("blocks install when storage validation has a problem", () => {
    expect(
      canInstall({
        loaded: true,
        storageLocation: "/home/user/BioFlow",
        storageValidation: BAD_STORAGE,
        portValidation: OK_PORT,
      }),
    ).toBe(false);
  });

  it("blocks install when port validation has a problem", () => {
    expect(
      canInstall({
        loaded: true,
        storageLocation: "/home/user/BioFlow",
        storageValidation: OK_STORAGE,
        portValidation: BAD_PORT,
      }),
    ).toBe(false);
  });

  it("blocks install when both storage and port have problems", () => {
    expect(
      canInstall({
        loaded: true,
        storageLocation: "/home/user/BioFlow",
        storageValidation: BAD_STORAGE,
        portValidation: BAD_PORT,
      }),
    ).toBe(false);
  });
});

describe("setupStatusText", () => {
  it("reads as not-yet-installed with no problems", () => {
    expect(setupStatusText({ storageProblem: false, portProblem: false })).toBe(
      "First run · not yet installed",
    );
  });

  it("singularizes for exactly one problem", () => {
    expect(setupStatusText({ storageProblem: true, portProblem: false })).toBe(
      "First run · 1 thing to fix",
    );
    expect(setupStatusText({ storageProblem: false, portProblem: true })).toBe(
      "First run · 1 thing to fix",
    );
  });

  it("pluralizes for both problems", () => {
    expect(setupStatusText({ storageProblem: true, portProblem: true })).toBe(
      "First run · 2 things to fix",
    );
  });
});

describe("storageLocationChanged", () => {
  it("is false when the value matches what's already installed", () => {
    expect(storageLocationChanged("/home/user/BioFlow", "/home/user/BioFlow")).toBe(false);
  });

  it("is true once the field diverges from the installed value", () => {
    expect(storageLocationChanged("/home/user/BioFlow", "/mnt/external/BioFlow")).toBe(true);
  });
});
