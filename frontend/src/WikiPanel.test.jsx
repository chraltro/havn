import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const listWikiPages = vi.fn();
const getWikiPage = vi.fn();

vi.mock("./api", () => ({
  api: {
    listWikiPages: (...args) => listWikiPages(...args),
    getWikiPage: (...args) => getWikiPage(...args),
  },
}));

const { default: WikiPanel } = await import("./WikiPanel");

beforeEach(() => {
  listWikiPages.mockReset();
  getWikiPage.mockReset();
  getWikiPage.mockImplementation(async (slug) => ({ slug, content: `# ${slug}` }));
});

describe("WikiPanel sidebar", () => {
  it("lists pages whose category is not in the fixed order", async () => {
    listWikiPages.mockResolvedValue([
      { slug: "index", title: "Welcome", category: "Getting Started" },
      { slug: "faq", title: "FAQ" }, // no category: grouped under "Other"
      { slug: "recipes", title: "Recipes", category: "Cookbook" },
    ]);
    render(<WikiPanel />);

    expect(await screen.findByRole("button", { name: "FAQ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recipes" })).toBeInTheDocument();
    expect(screen.getByText("Other")).toBeInTheDocument();
    expect(screen.getByText("Cookbook")).toBeInTheDocument();
  });

  it("renders page links as buttons that load the page", async () => {
    listWikiPages.mockResolvedValue([
      { slug: "index", title: "Welcome", category: "Getting Started" },
      { slug: "masking", title: "Masking", category: "Security" },
    ]);
    render(<WikiPanel />);

    const link = await screen.findByRole("button", { name: "Masking" });
    fireEvent.click(link);
    await waitFor(() => expect(getWikiPage).toHaveBeenCalledWith("masking"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Masking" })).toHaveAttribute("aria-current", "page"),
    );
  });
});
