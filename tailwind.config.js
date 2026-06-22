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
// Fonts are loaded from Google Fonts in ``base.html`` and bound here so
// utilities like ``font-plex-sans`` resolve to the right family stack.
module.exports = {
  content: ["./app/templates/**/*.html"],
  theme: {
    extend: {
      colors: {
        ink: "#1B2A4A",
        paper: "#F1F2EC",
        folio: "#FFFFFF",
        sello: "#B23B2E",
        registro: "#2F6F62",
        linea: "#D7D2C2",
        "texto-sec": "#5B6478",
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
