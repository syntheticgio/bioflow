import { describe, expect, it, vi } from "vitest";

import { clickableRow, isActivationKey } from "./clickableRow";

describe("isActivationKey", () => {
  it("activates on Enter and Space, the two a real button responds to", () => {
    expect(isActivationKey("Enter")).toBe(true);
    expect(isActivationKey(" ")).toBe(true);
  });

  it("ignores keys that must keep their normal meaning", () => {
    // Arrows scroll the panel and Tab moves focus. A row that swallowed them
    // would be worse to navigate than one that ignores the keyboard entirely.
    for (const key of ["Tab", "Escape", "ArrowDown", "ArrowUp", "a", "Spacebar"]) {
      expect(isActivationKey(key)).toBe(false);
    }
  });
});

describe("clickableRow", () => {
  it("exposes the row as a button that can hold focus", () => {
    const props = clickableRow(() => {});
    expect(props.role).toBe("button");
    expect(props.tabIndex).toBe(0);
  });

  it("activates on Enter and suppresses the default", () => {
    const onActivate = vi.fn();
    const preventDefault = vi.fn();
    clickableRow(onActivate).onKeyDown({
      key: "Enter",
      preventDefault,
    } as never);
    expect(onActivate).toHaveBeenCalledOnce();
    expect(preventDefault).toHaveBeenCalledOnce();
  });

  it("activates on Space and suppresses the page scroll it would cause", () => {
    // preventDefault is load-bearing here, not decoration: without it Space
    // scrolls the panel out from under the row the user just activated.
    const onActivate = vi.fn();
    const preventDefault = vi.fn();
    clickableRow(onActivate).onKeyDown({ key: " ", preventDefault } as never);
    expect(onActivate).toHaveBeenCalledOnce();
    expect(preventDefault).toHaveBeenCalledOnce();
  });

  it("does nothing on any other key", () => {
    const onActivate = vi.fn();
    const preventDefault = vi.fn();
    for (const key of ["Tab", "ArrowDown", "x"]) {
      clickableRow(onActivate).onKeyDown({ key, preventDefault } as never);
    }
    expect(onActivate).not.toHaveBeenCalled();
    expect(preventDefault).not.toHaveBeenCalled();
  });
});
