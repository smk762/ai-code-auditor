/*! dh-strip v1.2.12 | TypeScript declarations for dh-strip.mjs (vendored alongside it; a .mjs import resolves .d.mts). */

/** A site id used in data-dh-site: the lab home or one of the demos. */
export type DhSite = "dragonhound" | "lightbox" | "studio" | "atlas" | "cull" | "realms" | "safelight" | "assay" | "sightline" | "mask" | "quarry" | "proof" | "iris" | "retina";

/** A label-suffix key used in data-dh-label (1.2.4): "recorded" (the default) or "browser". */
export type DhLabel = "recorded" | "browser";

export interface DhStripLink {
  readonly site: DhSite;
  readonly label: string;
  readonly href: string;
}

export interface DhStrip {
  /** aria-label of the nav landmark. */
  readonly ariaLabel: string;
  /** The "Dragonhound Labs" home link. */
  readonly home: DhStripLink;
  /** The demo links, in display order. */
  readonly links: readonly DhStripLink[];
  /** "Live demo": always shown. */
  readonly label: string;
  /** The default label suffix, " · recorded output" (= labelSuffixes[defaultLabel]). Visually hidden on phones
   *  (below 1112 px since 1.2.11; 1072 px in 1.2.10, 1024 px in 1.2.8, 968 px in 1.2.7, 880 px in 1.2.5, 720 px in 1.2.4, 640 px in 1.2.2-1.2.3, 560 px before). */
  readonly labelMore: string;
  /** The key of the default suffix: "recorded". */
  readonly defaultLabel: DhLabel;
  /** Every label suffix by key (1.2.4): a site picks one with data-dh-label, or renders it natively. */
  readonly labelSuffixes: Readonly<Record<DhLabel, string>>;
  /** The mark's path data for viewBox 0 0 100 100, fill-rule evenodd. */
  readonly markPath: string;
}

/** Plain, frozen data for rendering the strip natively (React/Next). */
export declare const strip: DhStrip;

/** The strip as an HTML string; `current` adds aria-current="page" to that site's link. `label` renders only that
 *  suffix; without it the HTML carries every suffix (the default shown) for data-dh-label to pick from. */
export declare function stripHtml(options?: { current?: DhSite | string; label?: DhLabel }): string;

/** CSS selector list matching the current site's link from data-dh-site on the html element or the strip. */
export declare function currentSiteSelector(): string;

/** CSS rules that show the suffix data-dh-label picks (on the html element or the strip) and hide the default. */
export declare function labelSuffixCss(): string;
