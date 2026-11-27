import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const setSecret = vi.fn();
const runStream = vi.fn();

vi.mock("./api", () => ({
  api: {
    getScheduler: () => Promise.resolve({ streams: [{ name: "nightly", schedule: "0 6 * * *" }] }),
    getHistory: () => Promise.resolve([]),
    runStream: (...args) => runStream(...args),
    listSecrets: () => Promise.resolve([{ key: "DB_PASSWORD", masked_value: "****" }]),
    setSecret: (...args) => setSecret(...args),
    listUsers: () => Promise.resolve([]),
    getAlertHistory: () => Promise.resolve([]),
    getLintConfig: () => Promise.resolve({ exists: false }),
    getDatabaseConfig: () => Promise.resolve({}),
  },
}));
vi.mock("./ThemeProvider", () => ({
  useTheme: () => ({ colorThemeId: "havn-dark", fontThemeId: "outfit-jetbrains", setColorThemeId: () => {}, setFontThemeId: () => {} }),
}));

const { default: SettingsPanel } = await import("./SettingsPanel");

beforeEach(() => {
  setSecret.mockReset().mockResolvedValue({});
  runStream.mockReset();
});

function addSecret(key) {
  fireEvent.change(screen.getByLabelText("Secret key"), { target: { value: key } });
  fireEvent.change(screen.getByLabelText("Secret value"), { target: { value: "hunter2" } });
  fireEvent.click(screen.getByRole("button", { name: "Add secret" }));
}

describe("SettingsPanel", () => {
  it("asks before overwriting a secret that already exists", async () => {
    const showConfirm = vi.fn().mockResolvedValue(false);
    render(<SettingsPanel showConfirm={showConfirm} />);
    await screen.findByText("DB_PASSWORD");

    addSecret("DB_PASSWORD");

    await waitFor(() => expect(showConfirm).toHaveBeenCalledTimes(1));
    expect(showConfirm.mock.calls[0][1]).toContain("DB_PASSWORD");
    expect(setSecret).not.toHaveBeenCalled();
  });

  it("adds a new secret without asking", async () => {
    const showConfirm = vi.fn();
    render(<SettingsPanel showConfirm={showConfirm} />);
    await screen.findByText("DB_PASSWORD");

    addSecret("API_TOKEN");

    await waitFor(() => expect(setSecret).toHaveBeenCalledWith("API_TOKEN", "hunter2"));
    expect(showConfirm).not.toHaveBeenCalled();
  });

  it("shows why a scheduler Run Now failed", async () => {
    runStream.mockRejectedValue(new Error("a pipeline is already running"));
    render(<SettingsPanel showConfirm={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Run Now" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("nightly");
    expect(alert.textContent).toContain("a pipeline is already running");
  });
});
