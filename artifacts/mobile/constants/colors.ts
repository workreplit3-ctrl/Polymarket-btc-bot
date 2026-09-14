/**
 * Semantic design tokens for the mobile app.
 *
 * These tokens mirror the naming conventions used in web artifacts (index.css)
 * so that multi-artifact projects share a cohesive visual identity.
 *
 * Replace the placeholder values below with values that match the project's
 * brand. If a sibling web artifact exists, read its index.css and convert the
 * HSL values to hex so both artifacts use the same palette.
 *
 * To add dark mode, add a `dark` key with the same token names.
 * The useColors() hook will automatically pick it up.
 */

const colors = {
  light: {
    // Legacy aliases (kept for backward compatibility)
    text: '#eff4f6',
    tint: '#baf36a',

    // Core surfaces
    background: '#0b1115',
    foreground: '#eff4f6',

    // Cards / elevated surfaces
    card: '#121c22',
    cardForeground: '#eff4f6',

    // Primary action color (buttons, links, active states)
    primary: '#baf36a',
    primaryForeground: '#0b1115',

    // Secondary / less-emphasis interactive surfaces
    secondary: '#1b2b32',
    secondaryForeground: '#c8d3d8',

    // Muted / subdued elements (dividers, timestamps, placeholders)
    muted: '#16252c',
    mutedForeground: '#84959e',

    // Accent highlights (badges, selected items, focus rings)
    accent: '#17352d',
    accentForeground: '#baf36a',

    // Destructive actions (delete, error states)
    destructive: '#ff6b6b',
    destructiveForeground: '#160d0e',
    warning: '#f7bf62',
    warningForeground: '#211606',

    // Borders and input outlines
    border: '#263941',
    input: '#263941',
  },

  // Border radius (in px). Sync from the sibling web artifact's --radius
  // CSS variable. This value applies to cards, buttons, inputs, and modals.
  radius: 18,
};

export default colors;
