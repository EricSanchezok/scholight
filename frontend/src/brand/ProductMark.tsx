import type { CSSProperties } from "react";

import { brandAssets, brandMarkSources } from "../app/assets";

export type ProductMarkProps = {
  size?: number | string;
  alt?: string;
  className?: string;
  decorative?: boolean;
  priority?: boolean;
};

/**
 * The one Scholight product artwork. Every responsive size is a deterministic
 * raster export of the same lynx master; this component never swaps artwork.
 */
export function ProductMark({
  size = 128,
  alt = "Scholight lynx mark",
  className,
  decorative = false,
  priority = false,
}: ProductMarkProps) {
  const style: CSSProperties = { width: size, height: "auto", aspectRatio: "1 / 1" };
  return (
    <img
      className={["productMark", className].filter(Boolean).join(" ")}
      src={brandAssets.icon512}
      srcSet={brandMarkSources}
      sizes={typeof size === "number" ? `${size}px` : size}
      width={typeof size === "number" ? size : undefined}
      height={typeof size === "number" ? size : undefined}
      style={style}
      alt={decorative ? "" : alt}
      aria-hidden={decorative ? true : undefined}
      loading={priority ? "eager" : "lazy"}
      fetchPriority={priority ? "high" : "auto"}
      draggable={false}
    />
  );
}
