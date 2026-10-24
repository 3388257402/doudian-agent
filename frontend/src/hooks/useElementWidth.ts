import { useEffect, useRef, useState, type RefObject } from "react";

/**
 * 量出容器像素宽度，供 SVG 折线图按真实像素布局。
 * 为什么不用 viewBox 缩放：非等比缩放会把线宽压扁/拉粗、把圆点变成椭圆；
 * 用 preserveAspectRatio="none" 只是把失真藏起来。这里直接按像素画。
 */
export function useElementWidth<T extends HTMLElement>(fallback = 560): [RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const update = (next: number): void => {
      if (next > 0) setWidth(next);
    };
    update(node.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      update(entry.contentRect.width);
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
    };
  }, []);

  return [ref, width];
}
