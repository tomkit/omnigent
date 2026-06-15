import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bubble } from "@/lib/renderItems";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { useChatStore } from "@/store/chatStore";
import { BubbleView, PinnedMessageContext, usePinnedMessageJump } from "./ChatPage";

// UserBubble's pin-to-top action is wired through PinnedMessageContext
// (not props) because BubbleView is memoized on bubble equality alone.
// These tests pin the action's gating rules: provider present, committed
// itemId, non-empty text.

const originalLoadMoreHistory = useChatStore.getState().loadMoreHistory;

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  useChatStore.setState({
    hasMoreHistory: false,
    loadingMoreHistory: false,
    oldestItemId: null,
    loadMoreHistory: originalLoadMoreHistory,
  });
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const FILE_VIEWER_NOOP = {
  openFile: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: null,
  workspaceHome: null,
};

function userBubble(
  text: string,
  overrides: Partial<Extract<Bubble, { kind: "user" }>> = {},
): Bubble {
  return {
    kind: "user" as const,
    itemId: "item_1",
    content: [{ type: "input_text" as const, text }],
    ...overrides,
  };
}

function renderBubble(
  bubble: Bubble,
  pinning: React.ComponentProps<typeof PinnedMessageContext.Provider>["value"],
) {
  return render(
    <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
      <PinnedMessageContext.Provider value={pinning}>
        <BubbleView bubble={bubble} />
      </PinnedMessageContext.Provider>
    </FileViewerContext.Provider>,
  );
}

describe("UserBubble pin action", () => {
  it("pins the message with its itemId and text on click", () => {
    const togglePin = vi.fn();
    renderBubble(userBubble("review PR #42"), { pinnedItemId: null, togglePin });

    fireEvent.click(screen.getByTestId("pin-user-message"));

    // togglePin must receive the bubble's canonical itemId (the jump anchor)
    // and its extracted text (the banner snippet). A wrong itemId means the
    // banner's click would jump nowhere; missing text means an empty banner.
    expect(togglePin).toHaveBeenCalledTimes(1);
    expect(togglePin).toHaveBeenCalledWith("item_1", "review PR #42");
  });

  it("labels the action 'Unpin from top' when this message is the pin", () => {
    renderBubble(userBubble("pinned message"), {
      pinnedItemId: "item_1",
      togglePin: () => {},
    });

    // MessageAction exposes the tooltip as the button's accessible name.
    // The toggle affordance must flip on the pinned message — a stuck
    // "Pin to top" label means pinnedItemId never reaches the bubble.
    expect(screen.getByRole("button", { name: "Unpin from top" })).toBeDefined();
  });

  it("hides the action without a provider (isolated render)", () => {
    render(
      <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
        <BubbleView bubble={userBubble("no provider")} />
      </FileViewerContext.Provider>,
    );

    // Null context means "no pin surface here" (e.g. isolated tests, future
    // embeds) — rendering a dead button would be worse than none.
    expect(screen.queryByTestId("pin-user-message")).toBeNull();
  });

  it("hides the action for optimistic sends (pend_* temp ids)", () => {
    renderBubble(userBubble("in flight", { itemId: "pend_3" }), {
      pinnedItemId: null,
      togglePin: () => {},
    });

    // A pin keyed on a temp id dangles once the server commits the real
    // item id; the action must wait for promotion. A visible button here
    // means the pend_ gate regressed.
    expect(screen.queryByTestId("pin-user-message")).toBeNull();
  });

  it("hides the action for attachment-only messages with no text", () => {
    renderBubble(
      userBubble("", {
        // pending: file_id renders as a chip, so no network fetch in jsdom.
        content: [{ type: "input_image", file_id: "pending:shot.png", filename: "shot.png" }],
      }),
      { pinnedItemId: null, togglePin: () => {} },
    );

    // No text → nothing to show in the banner; the action must not offer a
    // pin that would render an empty pill.
    expect(screen.queryByTestId("pin-user-message")).toBeNull();
  });

  it("renders no pin action on system-message markers", () => {
    renderBubble(userBubble("[System: timer t1 fired]"), {
      pinnedItemId: null,
      togglePin: () => {},
    });

    // `[System: ...]` user-role items render as muted markers via
    // SystemMessageView — no user bubble, so no pin affordance either.
    expect(screen.queryByTestId("pin-user-message")).toBeNull();
  });
});

describe("usePinnedMessageJump", () => {
  it("keeps one banner click pending until older history renders the pinned message", async () => {
    vi.useFakeTimers();
    const scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    const loadMoreHistory = vi.fn(async () => {});
    const flashUserMessage = vi.fn();
    const onMissing = vi.fn();
    useChatStore.setState({ loadMoreHistory });
    document.body.innerHTML = '<div data-user-message-id="newer">newer</div>';

    const { result, rerender } = renderHook(
      ({
        userMessageIds,
        hasMoreHistory,
        loadingMoreHistory,
      }: {
        userMessageIds: string[];
        hasMoreHistory: boolean;
        loadingMoreHistory: boolean;
      }) =>
        usePinnedMessageJump(
          userMessageIds,
          hasMoreHistory,
          loadingMoreHistory,
          flashUserMessage,
          onMissing,
        ),
      {
        initialProps: {
          userMessageIds: ["newer"],
          hasMoreHistory: true,
          loadingMoreHistory: false,
        },
      },
    );

    await act(async () => {
      result.current("pinned");
      await Promise.resolve();
    });
    expect(loadMoreHistory).toHaveBeenCalledTimes(1);
    document.body.insertAdjacentHTML(
      "afterbegin",
      '<div data-user-message-id="pinned">pinned</div>',
    );
    rerender({
      userMessageIds: ["pinned", "newer"],
      hasMoreHistory: false,
      loadingMoreHistory: false,
    });

    // The first click must be enough: once the loaded history rerenders with
    // the pinned id, the pending jump scrolls automatically and keeps
    // correcting through the short layout-settle window. If this fails, the
    // UI regressed to "first click loads, second click jumps."
    act(() => vi.advanceTimersByTime(0));
    expect(scrollSpy).toHaveBeenCalledTimes(1);
    act(() => vi.runAllTimers());
    expect(scrollSpy).toHaveBeenCalledTimes(4);
    const target = scrollSpy.mock.contexts[0] as Element;
    expect(target.getAttribute("data-user-message-id")).toBe("pinned");
    expect(flashUserMessage).toHaveBeenCalledWith("pinned");
    // The target was found, so the pin is live — onMissing must NOT fire.
    // A spurious call here would unpin a perfectly reachable message.
    expect(onMissing).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("reports a pin as missing once all history is loaded without it", async () => {
    const scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    const loadMoreHistory = vi.fn(async () => {});
    const flashUserMessage = vi.fn();
    const onMissing = vi.fn();
    useChatStore.setState({ loadMoreHistory });
    document.body.innerHTML = '<div data-user-message-id="newer">newer</div>';

    // hasMoreHistory=false from the start: nothing left to page in, so the
    // requested id can never appear — the compacted/deleted-message case.
    const { result } = renderHook(() =>
      usePinnedMessageJump(["newer"], false, false, flashUserMessage, onMissing),
    );

    await act(async () => {
      result.current("gone");
      await Promise.resolve();
    });

    // With no more history to load, the hook must hand the dead id back to
    // onMissing exactly once so the caller can drop the pin. If this regresses,
    // a pin to a compacted message leaves a banner that jumps nowhere forever.
    expect(loadMoreHistory).not.toHaveBeenCalled();
    expect(onMissing).toHaveBeenCalledTimes(1);
    expect(onMissing).toHaveBeenCalledWith("gone");
    // Nothing was found, so there is no message to scroll to or flash.
    expect(scrollSpy).not.toHaveBeenCalled();
    expect(flashUserMessage).not.toHaveBeenCalled();
  });
});
