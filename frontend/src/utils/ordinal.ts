/**
 * v4.4.39 — render an integer as its English ordinal: 1 → "1st", 2 → "2nd",
 * 3 → "3rd", 11 → "11th", 12 → "12th", 13 → "13th", 21 → "21st", 112 → "112th".
 *
 * Used in the Providers list to make the priority field's lower-is-higher
 * semantics self-evident — "1st priority" reads correctly even without
 * knowing the lower=higher convention; "priority 1" historically did not.
 *
 * The math handles the 11/12/13 edge case (which break the standard
 * st/nd/rd pattern) via the `v - 20` modulo trick.
 */
export function ordinal(n: number): string {
  if (!Number.isFinite(n)) return String(n)
  const s = ['th', 'st', 'nd', 'rd']
  const v = n % 100
  return n + (s[(v - 20) % 10] || s[v] || s[0])
}
