/**
 * Format one integer minor-unit value. No currency is implied: the API's schema
 * is cents and the console does not invent a symbol or a locale.
 */
export function money(cents: number): string {
  return (cents / 100).toFixed(2);
}

/** Format an integer count for a tile or a table cell. */
export function count(value: number): string {
  return new Intl.NumberFormat("en-GB").format(value);
}
