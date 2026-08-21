import "@testing-library/jest-dom/vitest";

/**
 * jsdom has no layout engine, so recharts' `ResponsiveContainer` — which
 * measures its parent via `ResizeObserver` — never gets a real size and
 * renders zero points. A minimal stub reporting a fixed size is enough
 * for chart tests to render actual dots.
 */
class ResizeObserverStub {
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }

  observe(target: Element) {
    this.callback(
      [{ target, contentRect: { width: 600, height: 200 } } as ResizeObserverEntry],
      this as unknown as ResizeObserver
    );
  }

  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
