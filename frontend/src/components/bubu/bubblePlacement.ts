export type BubuRect = { left: number; top: number; width: number; height: number };
export type BubblePlacement = "top" | "bottom" | "left" | "right";
export type BubbleLayout = { left: number; top: number; placement: BubblePlacement; tail: number };

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(value, Math.max(min, max)));

/**
 * Anchors a bubble of `size` next to Bubu: above by default, flipping below, then
 * left/right, whichever first has room; otherwise the roomiest side. `tail` is the
 * offset of the pointer along the bubble edge that faces Bubu.
 */
export function placeBubble(
  anchor: BubuRect,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
  options: { gap?: number; margin?: number; tailInset?: number } = {}
): BubbleLayout {
  const { gap = 8, margin = 12, tailInset = 22 } = options;
  const space: Record<BubblePlacement, number> = {
    top: anchor.top - margin,
    bottom: viewport.height - (anchor.top + anchor.height) - margin,
    left: anchor.left - margin,
    right: viewport.width - (anchor.left + anchor.width) - margin,
  };
  const need = (placement: BubblePlacement) => (placement === "top" || placement === "bottom" ? size.height : size.width) + gap;
  const order: BubblePlacement[] = ["top", "bottom", "left", "right"];
  const placement = order.find((candidate) => space[candidate] >= need(candidate))
    ?? order.reduce((best, candidate) => (space[candidate] - need(candidate) > space[best] - need(best) ? candidate : best));

  const centerX = anchor.left + anchor.width / 2;
  const centerY = anchor.top + anchor.height / 2;
  let left: number;
  let top: number;
  if (placement === "top" || placement === "bottom") {
    left = clamp(centerX - size.width / 2, margin, viewport.width - size.width - margin);
    top = placement === "top" ? anchor.top - gap - size.height : anchor.top + anchor.height + gap;
    top = clamp(top, margin, viewport.height - size.height - margin);
  } else {
    top = clamp(centerY - size.height / 2, margin, viewport.height - size.height - margin);
    left = placement === "left" ? anchor.left - gap - size.width : anchor.left + anchor.width + gap;
    left = clamp(left, margin, viewport.width - size.width - margin);
  }
  const tail = placement === "top" || placement === "bottom"
    ? clamp(centerX - left, tailInset, size.width - tailInset)
    : clamp(centerY - top, tailInset, size.height - tailInset);
  return { left, top, placement, tail };
}
