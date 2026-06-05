import { useEffect, useRef } from 'react'
import { Modal, ModalHeader, ModalBody, ModalFooter } from './Modal'
import { Button } from './Button'

interface ConfirmDialogProps {
  open: boolean
  title: string
  message: string
  confirmLabel?: string
  variant?: 'danger' | 'primary'
  loading?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmDialog({
  open, title, message, confirmLabel = 'Confirm', variant = 'danger',
  loading, onConfirm, onCancel,
}: ConfirmDialogProps) {
  const confirmBtnRef = useRef<HTMLButtonElement | null>(null)

  // Auto-focus the confirm button when modal opens. With focus on a
  // <button>, the browser natively triggers click on Space or Enter,
  // which calls onConfirm via the onClick handler below. We also add
  // a document-level fallback in case focus gets lost (e.g. operator
  // tabbed to the backdrop or the close X).
  useEffect(() => {
    if (!open) return
    // Defer focus to next tick so the modal mount completes first.
    const t = setTimeout(() => confirmBtnRef.current?.focus(), 0)

    const onKey = (e: KeyboardEvent) => {
      if (loading) return
      if (e.key !== 'Enter' && e.key !== ' ') return
      const active = document.activeElement
      // Don't intercept space/enter inside form fields (none in this
      // dialog today, but defensive for future variants that add an
      // input — e.g. "type the username to confirm").
      if (active instanceof HTMLInputElement) return
      if (active instanceof HTMLTextAreaElement) return
      if (active instanceof HTMLSelectElement) return
      if (active && (active as HTMLElement).isContentEditable) return
      // If Cancel is focused, let its native handler win (Enter on
      // Cancel cancels, which is fine). Only intercept when focus is
      // elsewhere or on the Confirm button.
      if (active && (active as HTMLElement).innerText?.trim() === 'Cancel') return
      e.preventDefault()
      onConfirm()
    }
    document.addEventListener('keydown', onKey)
    return () => {
      clearTimeout(t)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, loading, onConfirm])

  return (
    <Modal open={open} onClose={onCancel} size="sm">
      <ModalHeader onClose={onCancel}>{title}</ModalHeader>
      <ModalBody>
        <p className="text-sm text-gray-600 dark:text-gray-400">{message}</p>
      </ModalBody>
      <ModalFooter>
        <Button variant="outline" onClick={onCancel}>Cancel</Button>
        <Button
          ref={confirmBtnRef}
          variant={variant}
          loading={loading}
          onClick={onConfirm}
        >{confirmLabel}</Button>
      </ModalFooter>
    </Modal>
  )
}
