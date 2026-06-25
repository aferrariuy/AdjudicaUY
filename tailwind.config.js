/** @type {import('tailwindcss').Config} */
//
// Design tokens for the AdjudicaUY rebrand. These mirror the mockup:
//   * ``ink``      — primary text and dark surfaces
//   * ``paper``    — page background
//   * ``folio``    — card / panel surface
//   * ``sello``    — accent (links, emphasis)
//   * ``registro`` — success / positive metrics
//   * ``linea``    — borders, hairlines, double rules
//   * ``texto-sec``— secondary / muted text
//
// ``ranking-badge`` is a nested palette (bg / text) that keeps the
// amber-on-amber treatment of the ranking numerics while still
// following the theme switch — its two halves swap roles in dark mode.
//
// Each color is mapped to ``rgb(var(--color-X) / <alpha-value>)`` so
// the token values defined in ``static/src/input.css`` resolve at
// runtime. The ``<alpha-value>`` placeholder lets Tailwind's opacity
// modifiers (``bg-sello/20``, ``text-ink/80``) work unchanged.
// ``darkMode: 'class'`` enables the ``.dark`` selector — toggled from
// the header button in ``base.html``.
//
// Fonts are loaded from Google Fonts in ``base.html`` and bound here so
// utilities like ``font-plex-sans`` resolve to the right family stack.
module.exports = {
  darkMode: 'class',
  content: ["./app/templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        ink: "rgb(var(--color-ink) / <alpha-value>)",
        paper: "rgb(var(--color-paper) / <alpha-value>)",
        folio: "rgb(var(--color-folio) / <alpha-value>)",
        sello: "rgb(var(--color-sello) / <alpha-value>)",
        registro: "rgb(var(--color-registro) / <alpha-value>)",
        linea: "rgb(var(--color-linea) / <alpha-value>)",
        "texto-sec": "rgb(var(--color-texto-sec) / <alpha-value>)",
        "ranking-badge": {
          bg: "rgb(var(--color-ranking-badge-bg) / <alpha-value>)",
          text: "rgb(var(--color-ranking-badge-text) / <alpha-value>)",
        },
      },
      fontFamily: {
        "big-shoulders": ['"Big Shoulders Display"', "serif"],
        "ibm-plex-mono": ['"IBM Plex Mono"', "ui-monospace", "monospace"],
        "ibm-plex": ['"IBM Plex Sans"', "system-ui", "sans-serif"],
        // Override Tailwind's default ``sans`` stack so body text
        // inherits Plex Sans without per-element class additions.
        sans: ['"IBM Plex Sans"', "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
