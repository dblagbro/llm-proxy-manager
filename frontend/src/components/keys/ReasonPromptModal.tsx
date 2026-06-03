// v5.0.0 — Reason prompt modal. Per decision 6, any policy change to
// blocked_companies or allowed_paths on an API key must be accompanied
// by a human-readable reason. The reason is persisted to
// compliance_policy_changes and surfaced in MyCompliancePage.
import { useState } from 'react'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
import { Button } from '@/components/ui/Button'

interface Props {
  open: boolean
  title?: string
  summary: string
  onCancel: () => void
  onConfirm: (reason: string) => void
  loading?: boolean
}

export function ReasonPromptModal({
  open, title = 'Policy change reason',
  summary, onCancel, onConfirm, loading,
}: Props) {
  const [reason, setReason] = useState('')

  function handleConfirm() {
    const r = reason.trim()
    if (!r) return
    onConfirm(r)
  }

  return (
    <Modal open={open} onClose={onCancel} size="md">
      <ModalHeader onClose={onCancel}>{title}</ModalHeader>
      <ModalBody>
        <p className="text-sm text-gray-600 dark:text-gray-300 mb-3">{summary}</p>
        <p className="text-xs text-gray-400 mb-2">
          A reason is required for compliance-policy edits (decision 6). It is
          logged into the policy-change audit log and shown to the caller.
        </p>
        <label htmlFor="policy-change-reason" className="sr-only">
          Reason for change
        </label>
        <textarea
          id="policy-change-reason"
          value={reason}
          onChange={e => setReason(e.target.value)}
          rows={3}
          placeholder="e.g. customer requires no Anthropic routing for FedRAMP review."
          className="w-full px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
        />
      </ModalBody>
      <ModalFooter>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button
          onClick={handleConfirm}
          disabled={!reason.trim()}
          loading={loading}
        >
          Save change
        </Button>
      </ModalFooter>
    </Modal>
  )
}
