// Tests for the Settings content panel. The section nav lives in the sidebar
// card (see settingsNav); the page renders only the section named by the URL.
// Covers the Appearance theme picker, the auth-gated Account section, and the
// Archived sessions list (which moved here out of the sidebar).

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

interface BuildInfoMock {
  version: string | null;
  sha: string | null;
  build_time: string | null;
  started_at: string | null;
  ref: string | null;
}

const NULL_BUILD: BuildInfoMock = {
  version: null,
  sha: null,
  build_time: null,
  started_at: null,
  ref: null,
};

const mocks = vi.hoisted(() => ({
  setTheme: vi.fn(),
  theme: "system" as string,
  archiveMutate: vi.fn(),
  deleteMutate: vi.fn(),
  accountsEnabled: true,
  me: { id: "alice", is_admin: false } as { id: string; is_admin: boolean } | null,
  conversations: [] as Conversation[],
  build: {
    version: null,
    sha: null,
    build_time: null,
    started_at: null,
    ref: null,
  } as BuildInfoMock,
}));

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: mocks.theme, systemTheme: "light", setTheme: mocks.setTheme }),
}));
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => false }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ accounts_enabled: mocks.accountsEnabled, build: mocks.build }),
}));
vi.mock("@/lib/accountsApi", () => ({
  getMe: () => Promise.resolve(mocks.me),
  logout: vi.fn(),
  changePassword: vi.fn(),
}));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: () => ({
    data: { pages: [{ data: mocks.conversations }] },
    isLoading: false,
  }),
  useArchiveConversation: () => ({ mutate: mocks.archiveMutate, isPending: false }),
  useStopAndDeleteConversation: () => ({ mutate: mocks.deleteMutate, isPending: false }),
}));

import { SettingsPage } from "./SettingsPage";

function conv(id: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    ...partial,
  };
}

function renderPage(path = "/settings") {
  return render(
    <TooltipProvider>
      <MemoryRouter initialEntries={[path]}>
        <SettingsPage />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

beforeEach(() => {
  mocks.setTheme.mockReset();
  mocks.archiveMutate.mockReset();
  mocks.deleteMutate.mockReset();
  mocks.theme = "system";
  mocks.accountsEnabled = true;
  mocks.me = { id: "alice", is_admin: false };
  mocks.conversations = [];
  mocks.build = { ...NULL_BUILD };
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("SettingsPage", () => {
  it("renders the Appearance section and applies a theme on card click", () => {
    renderPage("/settings/appearance");
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
    // System is selected (theme = "system").
    expect(screen.getByTestId("theme-system")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByTestId("theme-dark"));
    expect(mocks.setTheme).toHaveBeenCalledWith("dark");
  });

  it("defaults bare /settings to Account when accounts is on, else Appearance", async () => {
    // Accounts on → Account leads, so /settings lands on it.
    renderPage("/settings");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    // Accounts off → no Account section; default falls back to Appearance.
    cleanup();
    mocks.accountsEnabled = false;
    renderPage("/settings");
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
  });

  it("renders the Account section at /settings/account when auth is enabled", async () => {
    renderPage("/settings/account");
    await waitFor(() => expect(screen.getByText("alice")).toBeInTheDocument());

    // With accounts off, the section renders nothing even at its URL.
    cleanup();
    mocks.accountsEnabled = false;
    renderPage("/settings/account");
    expect(screen.queryByText("alice")).toBeNull();
  });

  it("lists archived sessions and unarchives on click", () => {
    mocks.conversations = [
      conv("conv_active"),
      conv("conv_archived", { archived: true, title: "Old chat" }),
    ];
    renderPage("/settings/archived");

    const rows = screen.getAllByTestId("archived-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Old chat")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("unarchive-conversation"));
    expect(mocks.archiveMutate).toHaveBeenCalledWith({ id: "conv_archived", archived: false });
  });

  it("deletes an archived session after confirming, with no row-click navigation", () => {
    mocks.conversations = [conv("conv_archived", { archived: true, title: "Old chat" })];
    renderPage("/settings/archived");

    // The row text isn't a link/button target — there's nothing to click into.
    expect(screen.queryByRole("link", { name: /Old chat/ })).toBeNull();

    // Trash → confirm dialog → Delete fires the delete mutation.
    fireEvent.click(screen.getByTestId("delete-archived"));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(mocks.deleteMutate).toHaveBeenCalledWith({ id: "conv_archived" });
  });

  it("renders Build & deployment provenance with a commit link and relative time", () => {
    // Freeze now 3 minutes after this instance started.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-30T12:00:00Z"));
    mocks.build = {
      version: "0.3.0.dev0",
      sha: "sha-ff243ad",
      build_time: "2026-06-30T11:30:00Z",
      started_at: "2026-06-30T11:57:00Z",
      ref: "main",
    };
    renderPage("/settings/about");

    expect(screen.getByRole("heading", { name: "Build & deployment" })).toBeInTheDocument();
    expect(screen.getByText("0.3.0.dev0")).toBeInTheDocument();

    // Commit renders as a link to the fork's commit page, sha- prefix stripped.
    const link = screen.getByTestId("build-commit-link");
    expect(link).toHaveTextContent("sha-ff243ad");
    expect(link).toHaveAttribute("href", "https://github.com/tomkit/omnigent/commit/ff243ad");

    // The git ref/branch the image was built from renders its own row.
    expect(screen.getByText("Ref")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();

    // "Last deployed" (process start) shows the 3-minutes-ago relative hint.
    expect(screen.getByText("(3m ago)")).toBeInTheDocument();
  });

  it("shows Unknown placeholders for an unstamped local/dev build", () => {
    mocks.build = { ...NULL_BUILD };
    renderPage("/settings/about");

    expect(screen.getByRole("heading", { name: "Build & deployment" })).toBeInTheDocument();
    // No commit link when sha is absent.
    expect(screen.queryByTestId("build-commit-link")).toBeNull();
    // Every field degrades to the muted "Unknown" placeholder (incl. Ref).
    expect(screen.getAllByText("Unknown").length).toBeGreaterThanOrEqual(5);
  });
});
