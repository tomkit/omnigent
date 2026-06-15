// Pinned-message banner stuck below the ChatHeader overlay at the top of
// the conversation viewport. Peer to WorkingStatusPin (which owns the
// bottom edge): absolute within Conversation, column-aligned with the chat
// content. Clicking the body jumps to the original message; ✕ unpins.

import { PinIcon, XIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export interface PinnedMessageBannerProps {
  /** Snippet of the pinned message (rendered as one CSS-truncated line). */
  text: string;
  /** Scroll the original message into view (banner body click). */
  onJump: () => void;
  /** Remove the pin (✕ button). */
  onUnpin: () => void;
  /** Column-width classes so the pill aligns with the chat content. */
  className?: string;
}

export function PinnedMessageBanner({
  text,
  onJump,
  onUnpin,
  className,
}: PinnedMessageBannerProps) {
  return (
    // top-16 clears the h-14 ChatHeader overlay with a small gap; z-20 sits
    // above the scroll content but below the header's z-30.
    <div
      className="pointer-events-none absolute inset-x-0 top-16 z-20"
      data-testid="pinned-message-banner"
    >
      {/* Mirror the conversation content column (mx-auto + px-6 + width) so
          the pill's left edge lines up with the message bubbles'. */}
      <div className={cn("mx-auto w-full px-6", className)}>
        <TooltipProvider>
          <div className="pointer-events-auto flex w-fit max-w-full items-center gap-0.5 rounded-full border border-border bg-card/95 py-1 pr-1 pl-3 shadow-sm backdrop-blur">
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={onJump}
                  className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
                  data-testid="pinned-message-jump"
                >
                  <PinIcon className="size-3 shrink-0" aria-hidden="true" />
                  <span className="truncate">{text}</span>
                </button>
              </TooltipTrigger>
              <TooltipContent>Jump to pinned message</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={onUnpin}
                  aria-label="Unpin message"
                  className="flex size-5 shrink-0 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  <XIcon className="size-3" aria-hidden="true" />
                </button>
              </TooltipTrigger>
              <TooltipContent>Unpin</TooltipContent>
            </Tooltip>
          </div>
        </TooltipProvider>
      </div>
    </div>
  );
}
