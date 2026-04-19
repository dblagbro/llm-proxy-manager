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
  return (
    <Modal open={open} onClose={onCancel} size="sm">
      <ModalHeader onClose={onCancel}>{title}</ModalHeader>
      <ModalBody>
        <p className="text-sm text-gray-600 dark:text-gray-400">{message}</p>
      </ModalBody>
      <ModalFooter>
        <Button variant="outline" onClick={onCancel}>Cancel</Button>
        <Button variant={variant} loading={loading} onClick={onConfirm}>{confirmLabel}</Button>
      </ModalFooter>
    </Modal>
  )
}
