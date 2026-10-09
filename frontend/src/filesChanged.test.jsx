import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

import { FILES_CHANGED_EVENT, notifyFilesChanged, useFilesChanged } from "./filesChanged";

function Listener({ onChanged }) {
  useFilesChanged(onChanged);
  return null;
}

describe("useFilesChanged", () => {
  it("runs the handler when files change, with the paths", () => {
    const onChanged = vi.fn();
    render(<Listener onChanged={onChanged} />);
    notifyFilesChanged(["transform/gold/orders.sql", "metrics/revenue.yml"]);
    expect(onChanged).toHaveBeenCalledWith(["transform/gold/orders.sql", "metrics/revenue.yml"]);
  });

  it("listens for the event the rename provider dispatches", () => {
    const onChanged = vi.fn();
    render(<Listener onChanged={onChanged} />);
    // The same event name Editor.jsx fires after a rename is applied.
    window.dispatchEvent(
      new CustomEvent("havn-files-changed", { detail: { paths: ["transform/silver/customers.sql"] } }),
    );
    expect(onChanged).toHaveBeenCalledWith(["transform/silver/customers.sql"]);
    expect(FILES_CHANGED_EVENT).toBe("havn-files-changed");
  });

  it("tolerates an event with no detail", () => {
    const onChanged = vi.fn();
    render(<Listener onChanged={onChanged} />);
    window.dispatchEvent(new CustomEvent("havn-files-changed"));
    expect(onChanged).toHaveBeenCalledWith([]);
  });

  it("picks up a handler swapped in without re-registering", () => {
    const first = vi.fn();
    const second = vi.fn();
    const view = render(<Listener onChanged={first} />);
    view.rerender(<Listener onChanged={second} />);
    notifyFilesChanged(["a.sql"]);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("stops listening once unmounted", () => {
    const onChanged = vi.fn();
    render(<Listener onChanged={onChanged} />).unmount();
    notifyFilesChanged(["a.sql"]);
    expect(onChanged).not.toHaveBeenCalled();
  });
});
