import { beforeEach, describe, expect, it } from "vitest";

import { notify, useMessageStore } from "./messageStore";

// The store is a module-level singleton; reset it so each test starts clean.
const fresh = () =>
  useMessageStore.setState({ messages: [], latest: null, error: null });

beforeEach(fresh);

describe("messageStore", () => {
  it("push adds to the front of the history and updates latest", () => {
    notify.info("first");
    notify.success("second");

    const s = useMessageStore.getState();
    expect(s.messages).toHaveLength(2);
    expect(s.messages[0].text).toBe("second");
    expect(s.messages[1].text).toBe("first");
    expect(s.latest?.text).toBe("second");
  });

  it("pins an error above later non-error messages (#890)", () => {
    notify.error("it broke");
    notify.info("QC queued");

    const s = useMessageStore.getState();
    expect(s.error?.text).toBe("it broke");
    expect(s.latest?.text).toBe("QC queued");
  });

  it("replaces a pinned error with a newer one", () => {
    notify.error("first failure");
    notify.error("second failure");

    expect(useMessageStore.getState().error?.text).toBe("second failure");
  });

  it("dismissError clears the pin but keeps the message in the log", () => {
    notify.error("it broke");
    useMessageStore.getState().dismissError();

    const s = useMessageStore.getState();
    expect(s.error).toBeNull();
    expect(s.messages[0].text).toBe("it broke");
  });

  it("clear empties the log, the latest, and the pin", () => {
    notify.error("it broke");
    notify.info("hello");
    useMessageStore.getState().clear();

    const s = useMessageStore.getState();
    expect(s.messages).toEqual([]);
    expect(s.latest).toBeNull();
    expect(s.error).toBeNull();
  });

  it("caps the history at 100", () => {
    for (let i = 0; i < 120; i++) notify.info(`m${i}`);

    const s = useMessageStore.getState();
    expect(s.messages).toHaveLength(100);
    expect(s.messages[0].text).toBe("m119");
  });
});
