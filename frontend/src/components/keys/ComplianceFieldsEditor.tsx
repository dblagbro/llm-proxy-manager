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
import { Plus, Trash2 } from 'lucide-react'
import { Switch } from '@/components/ui/Switch'
import { Button } from '@/components/ui/Button'
import { HelpHint } from '@/components/ui/HelpHint'
import { KNOWN_COMPANIES } from '@/types'

const PROD_CLI_PRESET = ['/v1/chat/completions', '/v1/models', '/health']

interface Props {
  blockedCompanies: string[]
  setBlockedCompanies: (v: string[]) => void
  allowedPaths: string[] | null
  setAllowedPaths: (v: string[] | null) => void
  debugEchoEnabled: boolean
  setDebugEchoEnabled: (v: boolean) => void
}

export function ComplianceFieldsEditor({
  blockedCompanies, setBlockedCompanies,
  allowedPaths, setAllowedPaths,
  debugEchoEnabled, setDebugEchoEnabled,
}: Props) {
  const [customCompany, setCustomCompany] = useState('')

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
    id => !KNOWN_COMPANIES.some(c => c.id === id),
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
          {KNOWN_COMPANIES.map(c => (
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
