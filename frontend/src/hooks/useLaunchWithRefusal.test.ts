import { describe, expect, it } from "vitest";

import { ApiRequestError } from "../api/client";
import { refusalFrom } from "./useLaunchWithRefusal";

function refusalError(details: Record<string, unknown>) {
  return new ApiRequestError(422, "resource_refused", "Too big", details);
}

describe("refusalFrom", () => {
  it("recognises a resource refusal and returns its details", () => {
    // Getting this wrong loses the "launch anyway" escape and dead-ends the
    // user on an error they could have overridden.
    const details = {
      refusal: "estimate",
      estimate_mb: 32000,
      budget_mb: 16000,
      detail: "needs more than the budget",
    };
    expect(refusalFrom(refusalError(details))).toEqual(details);
  });

  it("returns null for an ordinary API error", () => {
    // The other direction: offering an override that cannot help is its own
    // kind of wrong.
    expect(refusalFrom(new ApiRequestError(500, "internal", "boom", {}))).toBeNull();
    expect(
      refusalFrom(new ApiRequestError(404, "not_found", "gone", { object_id: "x" })),
    ).toBeNull();
  });

  it("returns null for a non-API error", () => {
    expect(refusalFrom(new Error("network down"))).toBeNull();
    expect(refusalFrom("a string")).toBeNull();
    expect(refusalFrom(null)).toBeNull();
    expect(refusalFrom(undefined)).toBeNull();
  });

  it("keys off the details, not the status", () => {
    // A 422 without a refusal is an ordinary validation failure; a refusal is
    // identified by what the server sent, not by the code it sent it under.
    expect(
      refusalFrom(new ApiRequestError(422, "validation_error", "bad field", {})),
    ).toBeNull();
  });
});
