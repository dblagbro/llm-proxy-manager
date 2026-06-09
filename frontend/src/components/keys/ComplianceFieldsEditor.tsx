// v5.0.0 — Compliance fields editor used inside the Create/Edit API Key
// modal. Renders the three new ApiKey fields:
//   - blocked_companies (string[] | null) — multi-select with known 10
//     + a free-text "custom company id" input
//   - allowed_paths    (string[] | null)  — string array editor with
//     "production CLI preset" shortcut; null means unrestricted
//   - debug_echo_enabled (bool)           — gates /api/debug/echo-client
//
// Designed to be small and reusable; the parent owns state.
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Plus, Trash2 } from 'lucide-react'
import { Switch } from '@/components/ui/Switch'
import { Button } from '@/components/ui/Button'
import { HelpHint } from '@/components/ui/HelpHint'
import { KNOWN_COMPANIES, type CompanyChoice } from '@/types'
import { complianceApi } from '@/api'

// v5.3.2 — fetch the live taxonomy so operator-defined custom
// companies (COMPLIANCE_CUSTOM_COMPANIES env JSON) surface in the
// policy-editor dropdowns. Falls back to the static KNOWN_COMPANIES
// list during the first paint, on fetch error, and on legacy
// deployments where the endpoint hasn't shipped yet (defense-in-depth).
function useCompanyTaxonomy(): CompanyChoice[] {
  const { data } = useQuery({
    queryKey: ['compliance-taxonomy'],
    queryFn: complianceApi.taxonomy,
    staleTime: 5 * 60_000,  // 5 min — taxonomy is near-static
    retry: 1,
  })
  if (!data?.companies?.length) return KNOWN_COMPANIES
  return data.companies.map(c => ({ id: c.id, label: c.label }))
}

const PROD_CLI_PRESET = ['/v1/chat/completions', '/v1/models', '/health']

interface Props {
  blockedCompanies: string[]
  setBlockedCompanies: (v: string[]) => void
  allowedPaths: string[] | null
  setAllowedPaths: (v: string[] | null) => void
  debugEchoEnabled: boolean
  setDebugEchoEnabled: (v: boolean) => void
  // v5.3.0 — fine-grained policy fields. null = no per-key restriction
  // (system policy still applies). Non-empty allowedCompanies switches
  // that dimension to allowlist mode. Model lists accept exact names
  // OR fnmatch globs ("claude-*", "gpt-4-*-turbo").
  allowedCompanies: string[] | null
  setAllowedCompanies: (v: string[] | null) => void
  blockedModels: string[] | null
  setBlockedModels: (v: string[] | null) => void
  allowedModels: string[] | null
  setAllowedModels: (v: string[] | null) => void
}

export function ComplianceFieldsEditor({
  blockedCompanies, setBlockedCompanies,
  allowedPaths, setAllowedPaths,
  debugEchoEnabled, setDebugEchoEnabled,
  allowedCompanies, setAllowedCompanies,
  blockedModels, setBlockedModels,
  allowedModels, setAllowedModels,
}: Props) {
  const [customCompany, setCustomCompany] = useState('')
  // v5.3.2 — live taxonomy with KNOWN_COMPANIES fallback.
  const companies = useCompanyTaxonomy()

  function toggleCompany(id: string) {
    if (blockedCompanies.includes(id)) {
      setBlockedCompanies(blockedCompanies.filter(c => c !== id))
    } else {
      setBlockedCompanies([...blockedCompanies, id])
    }
  }

  function addCustomCompany() {
    const v = customCompany.trim().toLowerCase()
    if (!v) return
    if (blockedCompanies.includes(v)) {
      setCustomCompany('')
      return
    }
    setBlockedCompanies([...blockedCompanies, v])
    setCustomCompany('')
  }

  // Allowed paths state machine: null = unrestricted; [] = "no paths
  // allowed"; non-empty = whitelist. The toggle below flips between
  // null and the current array.
  const restrictionEnabled = allowedPaths !== null
  function toggleRestriction(on: boolean) {
    if (on) setAllowedPaths(allowedPaths ?? [])
    else setAllowedPaths(null)
  }

  function updatePath(i: number, value: string) {
    if (!allowedPaths) return
    const next = [...allowedPaths]
    next[i] = value
    setAllowedPaths(next)
  }

  function removePath(i: number) {
    if (!allowedPaths) return
    const next = allowedPaths.filter((_, idx) => idx !== i)
    setAllowedPaths(next)
  }

  function addPath() {
    setAllowedPaths([...(allowedPaths ?? []), ''])
  }

  function applyProdCliPreset() {
    setAllowedPaths(PROD_CLI_PRESET)
  }

  const customSelections = blockedCompanies.filter(
    id => !companies.some(c => c.id === id),
  )

  return (
    <div className="space-y-5 border-t border-gray-200 dark:border-gray-700 pt-4 mt-2">
      <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
        Compliance (v5.0.0)
      </p>

      {/* Blocked companies */}
      <div>
        <div className="flex items-center gap-1 mb-1.5">
          <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
            Blocked companies
          </label>
          <HelpHint text="The caller of this key will never be routed to any provider whose owner_company is on this list. Substitution applies first; if no compliant alternative exists the request gets a 451 (or model_not_available, depending on caller). Empty = no per-key block (system-wide block still applies)." />
        </div>
        <div
          role="group"
          aria-label="Blocked companies"
          className="grid grid-cols-2 gap-2"
        >
          {companies.map(c => (
            <label
              key={c.id}
              className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer"
            >
              <input
                type="checkbox"
                checked={blockedCompanies.includes(c.id)}
                onChange={() => toggleCompany(c.id)}
                aria-label={`Block ${c.label}`}
                className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
              />
              <span>{c.label}</span>
              <span className="text-xs text-gray-400 font-mono">{c.id}</span>
            </label>
          ))}
        </div>

        {customSelections.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {customSelections.map(id => (
              <span
                key={id}
                className="inline-flex items-center gap-1 text-xs bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-full px-2 py-0.5 font-mono"
              >
                {id}
                <button
                  type="button"
                  onClick={() => toggleCompany(id)}
                  aria-label={`Remove ${id}`}
                  className="text-gray-400 hover:text-red-500"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="mt-2 flex items-center gap-2">
          <label htmlFor="custom-company-input" className="sr-only">
            Add custom company ID
          </label>
          <input
            id="custom-company-input"
            type="text"
            value={customCompany}
            onChange={e => setCustomCompany(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addCustomCompany() } }}
            placeholder="add custom company id (lowercase)"
            className="flex-1 px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
          />
          <Button
            size="sm"
            variant="outline"
            type="button"
            onClick={addCustomCompany}
            disabled={!customCompany.trim()}
          >
            Add
          </Button>
        </div>
      </div>

      {/* Allowed paths */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <div className="flex items-center gap-1">
            <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
              Allowed paths
            </label>
            <HelpHint text="Whitelist of HTTP request paths this key may hit. Off (no list) = unrestricted, the default. When ON and the list is empty the key can't hit any endpoint. Paths are matched exactly (no globs). Most operators only set this on production CLI keys." />
          </div>
          <Switch
            checked={restrictionEnabled}
            onChange={toggleRestriction}
            ariaLabel="Restrict allowed paths"
          />
        </div>
        {restrictionEnabled && (
          <div className="space-y-2">
            {(allowedPaths ?? []).map((p, i) => (
              <div key={i} className="flex items-center gap-2">
                <label htmlFor={`allowed-path-${i}`} className="sr-only">
                  Allowed path {i + 1}
                </label>
                <input
                  id={`allowed-path-${i}`}
                  type="text"
                  value={p}
                  onChange={e => updatePath(i, e.target.value)}
                  placeholder="/v1/chat/completions"
                  className="flex-1 px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none font-mono"
                />
                <button
                  type="button"
                  onClick={() => removePath(i)}
                  aria-label={`Remove allowed path row ${i + 1}`}
                  className="p-1.5 text-gray-400 hover:text-red-500 rounded"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))}
            <div className="flex items-center gap-2">
              <Button size="sm" variant="outline" type="button" onClick={addPath}>
                <Plus className="h-3.5 w-3.5 mr-1" />Add row
              </Button>
              <Button
                size="sm"
                variant="ghost"
                type="button"
                onClick={applyProdCliPreset}
                title={`Sets: ${PROD_CLI_PRESET.join(', ')}`}
              >
                Production CLI preset
              </Button>
            </div>
            {(allowedPaths ?? []).length === 0 && (
              <p className="text-xs text-amber-500">
                With restriction ON and no paths listed, this key will be denied every endpoint.
              </p>
            )}
          </div>
        )}
      </div>

      {/* v5.3.0 — Allowed companies (allowlist mode) */}
      <CompanyAllowlistEditor
        value={allowedCompanies}
        setValue={setAllowedCompanies}
      />

      {/* v5.3.0 — Blocked models (deny wins) */}
      <ModelPatternEditor
        kind="blocked"
        value={blockedModels}
        setValue={setBlockedModels}
      />

      {/* v5.3.0 — Allowed models (allowlist mode) */}
      <ModelPatternEditor
        kind="allowed"
        value={allowedModels}
        setValue={setAllowedModels}
      />

      {/* Debug echo */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <p className="text-sm font-medium text-gray-700 dark:text-gray-300">
            Debug echo enabled
          </p>
          <p className="text-xs text-gray-400">
            Gates the sandbox /api/debug/echo-client endpoint. Leave OFF for production keys.
          </p>
        </div>
        <Switch
          checked={debugEchoEnabled}
          onChange={setDebugEchoEnabled}
          ariaLabel="Enable debug echo endpoint"
        />
      </div>
    </div>
  )
}


// ── v5.3.0 / Batch V2 UI ───────────────────────────────────────────
//
// CompanyAllowlistEditor mirrors the existing blocked-companies grid
// but with allowlist semantics: non-empty list switches that dimension
// to allowlist mode (only listed companies pass; everything else
// dropped). null = no allowlist active; [] is illegal (the toggle
// flips between null and a non-empty list as the operator picks).
function CompanyAllowlistEditor({
  value, setValue,
}: { value: string[] | null; setValue: (v: string[] | null) => void }) {
  const enabled = value !== null
  const list = value ?? []
  const [custom, setCustom] = useState('')
  // v5.3.2 — same live taxonomy as the parent; useQuery dedupes via
  // queryKey so this is one HTTP call regardless of how many
  // sub-pickers mount.
  const companies = useCompanyTaxonomy()

  function toggle(on: boolean) {
    setValue(on ? [] : null)
  }
  function toggleCompany(id: string) {
    if (list.includes(id)) setValue(list.filter(c => c !== id))
    else setValue([...list, id])
  }
  function addCustom() {
    const v = custom.trim().toLowerCase()
    if (!v || list.includes(v)) { setCustom(''); return }
    setValue([...list, v])
    setCustom('')
  }

  const customSelections = list.filter(
    id => !companies.some(c => c.id === id),
  )

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-1">
          <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
            Allowed companies (allowlist mode)
          </label>
          <HelpHint text="When ON, ONLY the listed companies are eligible for routing — every other company is dropped + audited as 'company-not-in-allowlist'. Blocked-companies still wins over this (deny is unconditional). Use for compliance-locked deployments that need positive enforcement instead of enumerating bans. OFF (default) preserves v5.0 behavior." />
        </div>
        <Switch checked={enabled} onChange={toggle} ariaLabel="Enable company allowlist" />
      </div>
      {enabled && (
        <>
          <div role="group" aria-label="Allowed companies" className="grid grid-cols-2 gap-2">
            {companies.map(c => (
              <label key={c.id} className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={list.includes(c.id)}
                  onChange={() => toggleCompany(c.id)}
                  aria-label={`Allow ${c.label}`}
                  className="h-4 w-4 rounded border-gray-300 text-emerald-600 focus:ring-emerald-500"
                />
                <span>{c.label}</span>
                <span className="text-xs text-gray-400 font-mono">{c.id}</span>
              </label>
            ))}
          </div>
          {customSelections.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {customSelections.map(id => (
                <span key={id} className="inline-flex items-center gap-1 text-xs bg-emerald-100 dark:bg-emerald-900/40 text-emerald-800 dark:text-emerald-300 rounded-full px-2 py-0.5 font-mono">
                  {id}
                  <button type="button" onClick={() => toggleCompany(id)} aria-label={`Remove ${id}`} className="text-emerald-600 hover:text-red-500">×</button>
                </span>
              ))}
            </div>
          )}
          <div className="mt-2 flex items-center gap-2">
            <input
              type="text"
              value={custom}
              onChange={e => setCustom(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addCustom() } }}
              placeholder="add custom company id (lowercase)"
              className="flex-1 px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500 focus:outline-none"
            />
            <Button size="sm" variant="outline" type="button" onClick={addCustom} disabled={!custom.trim()}>Add</Button>
          </div>
          {list.length === 0 && (
            <p className="text-xs text-amber-500 mt-2">
              Allowlist ON with no entries → every provider is dropped on every request.
            </p>
          )}
        </>
      )}
    </div>
  )
}

// Shared editor for blocked_models + allowed_models. Both accept
// list[str] of exact names OR fnmatch globs ("claude-*",
// "gpt-4-*-turbo"). null = no per-key restriction.
function ModelPatternEditor({
  kind, value, setValue,
}: {
  kind: 'blocked' | 'allowed'
  value: string[] | null
  setValue: (v: string[] | null) => void
}) {
  const enabled = value !== null
  const list = value ?? []
  const isBlocked = kind === 'blocked'

  function toggle(on: boolean) {
    setValue(on ? [] : null)
  }
  function updateRow(i: number, next: string) {
    const out = [...list]
    out[i] = next
    setValue(out)
  }
  function removeRow(i: number) {
    setValue(list.filter((_, j) => j !== i))
  }
  function addRow() {
    setValue([...list, ''])
  }

  const title = isBlocked ? 'Blocked models' : 'Allowed models (allowlist mode)'
  const hint = isBlocked
    ? 'Each entry is an exact model name OR an fnmatch glob ("claude-*", "gpt-4-*-turbo"). Match is case-insensitive against the provider\'s default_model AND the request\'s requested_model — defense in depth against misconfigured providers. Deny wins: a model in both blocked and allowed lists is BLOCKED.'
    : 'When ON, only models matching one of these patterns are eligible. fnmatch globs ("claude-*") and exact names both work. A non-matching provider OR a non-matching requested_model drops the candidate. blocked_models wins over this.'
  const accent = isBlocked ? 'red' : 'emerald'

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-1">
          <label className="text-sm font-medium text-gray-700 dark:text-gray-300">{title}</label>
          <HelpHint text={hint} />
        </div>
        <Switch checked={enabled} onChange={toggle} ariaLabel={`Enable ${kind} models`} />
      </div>
      {enabled && (
        <div className="space-y-2">
          {list.map((p, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                type="text"
                value={p}
                onChange={e => updateRow(i, e.target.value)}
                placeholder={isBlocked ? 'claude-opus-4-* or claude-3-5-sonnet' : 'gpt-*'}
                className="flex-1 px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:outline-none font-mono"
              />
              <button
                type="button"
                onClick={() => removeRow(i)}
                aria-label={`Remove ${kind} model row ${i + 1}`}
                className="p-1.5 text-gray-400 hover:text-red-500 rounded"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          ))}
          <Button size="sm" variant="outline" type="button" onClick={addRow}>
            <Plus className="h-3.5 w-3.5 mr-1" />Add pattern
          </Button>
          {list.length === 0 && !isBlocked && (
            <p className="text-xs text-amber-500">
              Allowlist ON with no patterns → every provider is dropped (the model gate never passes).
            </p>
          )}
          <p className={`text-xs text-${accent}-500 dark:text-${accent}-400`}>
            {isBlocked
              ? 'Empty list = block nothing (toggle OFF instead to be explicit).'
              : 'Patterns match case-insensitively against provider.default_model + requested_model.'}
          </p>
        </div>
      )}
    </div>
  )
}
