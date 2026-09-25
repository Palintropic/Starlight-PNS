// Press feedback in the spirit of UWP's PointerDownThemeAnimation: the pressed control sinks
// slightly and leans away from the pointer. The CSS in index.css does the animation; this only
// records where the press landed, as --press-x / --press-y in -1..1 on the pressed element.
//
// The values are left in place after release so the control unwinds along the axis it tilted
// on. A keyboard press never sets them, so it sinks without tilting.

const TILT_SELECTOR = '.btn, .toggle, .tilt';

function clamp(value: number): number {
  return Math.max(-1, Math.min(1, value));
}

export function installPressTilt(root: Document = document): () => void {
  const onPointerDown = (event: PointerEvent) => {
    const target = event.target instanceof Element ? event.target.closest<HTMLElement>(TILT_SELECTOR) : null;
    if (!target) return;
    const rect = target.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    const x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    const y = ((event.clientY - rect.top) / rect.height) * 2 - 1;
    target.style.setProperty('--press-x', clamp(x).toFixed(3));
    target.style.setProperty('--press-y', clamp(y).toFixed(3));
  };
  root.addEventListener('pointerdown', onPointerDown, { capture: true, passive: true });
  return () => root.removeEventListener('pointerdown', onPointerDown, { capture: true });
}
