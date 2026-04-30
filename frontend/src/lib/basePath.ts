/**
 * Runtime URL-prefix detection for path-relative deployment (v3.0.16).
 *
 * The SPA can be served at any URL prefix — `/llm-proxy2/`,
 * `/llm-proxy2-smoke/`, or any future stage — without rebuilding the
 * frontend bundle. This works by detecting the prefix at runtime from
 * the actual `<script src>` of the loaded JS module instead of relying
 * on Vite's build-time `base`.
 *
 * Used by:
 *   - `BrowserRouter basename={...}` in App.tsx (so route matching works
 *     regardless of mount path)
 *   - `api/client.ts` BASE prefix (so `/api/...` calls go to the right
 *     container)
 *
 * Companion change: `vite.config.ts` is now `base: './'` so emitted
 * `index.html` references assets via relative URLs that resolve to
 * whatever path served the page.
 */
export function getBasePath(): string {
  // Cache after first call so we read the DOM once.
  if (typeof _cached === "string") return _cached;

  // Prefer the path of the currently-executing module (most reliable —
  // works even if the page navigated client-side and `<script src>`
  // tags were re-arranged).
  try {
    const fromScripts = pathFromScriptSrc();
    if (fromScripts !== null) {
      _cached = fromScripts;
      return _cached;
    }
  } catch {
    /* fall through */
  }

  // Fallback — derive from window.location.pathname by stripping known
  // route segments. Useful during dev / SSR / unusual hosting.
  _cached = pathFromLocation();
  return _cached;
}

let _cached: string | null = null;

function pathFromScriptSrc(): string | null {
  if (typeof document === "undefined") return null;
  const scripts = Array.from(document.getElementsByTagName("script"));
  for (const s of scripts) {
    const src = s.src || "";
    if (!src) continue;
    // Vite-built bundle scripts live at <prefix>/assets/index-XXX.js
    const idx = src.indexOf("/assets/");
    if (idx <= 0) continue;
    try {
      const u = new URL(src);
      const dirIdx = u.pathname.indexOf("/assets/");
      if (dirIdx <= 0) continue;
      // Trim any trailing slash; basename expects no trailing slash.
      const prefix = u.pathname.substring(0, dirIdx);
      return prefix === "/" ? "" : prefix;
    } catch {
      continue;
    }
  }
  return null;
}

function pathFromLocation(): string {
  if (typeof window === "undefined") return "";
  const path = window.location.pathname;
  // Heuristic: anything before the first known route segment is the prefix.
  // The known SPA routes from App.tsx — keep in sync if you add new ones.
  const KNOWN_ROUTES = [
    "/login",
    "/providers",
    "/routing",
    "/keys",
    "/users",
    "/cluster",
    "/metrics",
    "/activity",
    "/settings",
  ];
  for (const route of KNOWN_ROUTES) {
    const idx = path.indexOf(route);
    if (idx >= 0) {
      const prefix = path.substring(0, idx);
      return prefix === "/" ? "" : prefix.replace(/\/$/, "");
    }
  }
  // No known route segment — assume the whole path is the prefix
  // (minus trailing slash and any index.html).
  return path.replace(/\/(index\.html)?$/, "");
}
