import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Edit2 } from 'lucide-react'
import { usersApi } from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
import { KeyRound } from 'lucide-react'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Spinner } from '@/components/ui/Spinner'
import { useToast } from '@/components/ui/Toast'
import { useAuth } from '@/context/AuthContext'
import type { User } from '@/types'

type Role = 'admin' | 'user'

export function UsersPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const { user: me } = useAuth()
  const [showModal, setShowModal] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [form, setForm] = useState({ username: '', password: '', role: 'user' as Role, email: '' })
  const [deleteId, setDeleteId] = useState<string | null>(null)
  // v5.22.7 option A — admin-initiated reset. The generated password is shown
  // exactly once; it is never stored in plaintext or returned again.
  const [resetUser, setResetUser] = useState<User | null>(null)
  const [tempPassword, setTempPassword] = useState<string | null>(null)
  // v5.0.22 — bulk-delete selection. Self is never selectable.
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false)

  const { data: users, isLoading } = useQuery({ queryKey: ['users'], queryFn: usersApi.list })

  const saveMutation = useMutation({
    mutationFn: () => editing
      ? usersApi.update(editing.id, { password: form.password || undefined, role: form.role, email: form.email })
      : usersApi.create({ username: form.username, password: form.password, role: form.role, email: form.email || undefined }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] })
      toast.success(editing ? 'User updated' : 'User created')
      closeModal()
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const resetMutation = useMutation({
    mutationFn: (id: string) => usersApi.resetPassword(id),
    onSuccess: (res: { temporary_password?: string }) => {
      setTempPassword(res.temporary_password || null)
      qc.invalidateQueries({ queryKey: ['users'] })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => usersApi.delete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['users'] }); toast.success('User deleted') },
    onError: (e: Error) => toast.error(e.message),
  })

  const bulkDeleteMutation = useMutation({
    mutationFn: (ids: string[]) => usersApi.bulkDelete(ids),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['users'] })
      setSelected(new Set())
      setBulkConfirmOpen(false)
      if (r.errors?.length) {
        toast.error(
          `Deleted ${r.deleted.length}; ${r.errors.length} skipped (` +
          r.errors.slice(0, 3).map(e => e.reason).join(', ') +
          (r.errors.length > 3 ? ', …' : '') + ')'
        )
      } else {
        toast.success(`Deleted ${r.deleted.length} user${r.deleted.length === 1 ? '' : 's'}`)
      }
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function openCreate() {
    setEditing(null)
    setForm({ username: '', password: '', role: 'user', email: '' })
    setShowModal(true)
  }

  function openEdit(u: User) {
    setEditing(u)
    setForm({ username: u.username, password: '', role: u.role as Role, email: u.email || '' })
    setShowModal(true)
  }

  function closeModal() { setShowModal(false); setEditing(null) }

  // Self is never selectable. Compute the set of selectable users.
  const selectableUsers = useMemo(
    () => (users ?? []).filter(u => u.username !== me?.username),
    [users, me?.username],
  )

  const allSelected = selectableUsers.length > 0 && selectableUsers.every(u => selected.has(u.id))
  const someSelected = !allSelected && selectableUsers.some(u => selected.has(u.id))

  function toggleSelectAll() {
    if (allSelected) {
      setSelected(new Set())
    } else {
      setSelected(new Set(selectableUsers.map(u => u.id)))
    }
  }

  function toggleOne(id: string) {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectedCount = selected.size

  return (
    <div className="p-6 space-y-6 max-w-4xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Users</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{users?.length ?? 0} users</p>
        </div>
        <div className="flex items-center gap-2">
          {selectedCount > 0 && (
            <Button
              size="sm"
              variant="danger"
              onClick={() => setBulkConfirmOpen(true)}
              loading={bulkDeleteMutation.isPending}
            >
              <Trash2 className="h-4 w-4 mr-1.5" />Delete {selectedCount} selected
            </Button>
          )}
          <Button size="sm" onClick={openCreate}><Plus className="h-4 w-4 mr-1.5" />Add User</Button>
        </div>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner /></div>
      ) : (
        <Card>
          <CardContent className="p-0">
            {/* Select-all header row */}
            {selectableUsers.length > 0 && (
              <div className="flex items-center gap-3 px-5 py-3 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
                <input
                  type="checkbox"
                  className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                  checked={allSelected}
                  ref={el => { if (el) el.indeterminate = someSelected }}
                  onChange={toggleSelectAll}
                  aria-label="Select all users"
                />
                <span className="text-xs font-medium text-gray-600 dark:text-gray-400">
                  {selectedCount > 0 ? `${selectedCount} selected` : 'Select all'}
                </span>
              </div>
            )}
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {(users ?? []).map(u => {
                const isSelf = u.username === me?.username
                return (
                  <div key={u.id} className="flex items-center gap-3 px-5 py-4">
                    <input
                      type="checkbox"
                      className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500 disabled:opacity-40 disabled:cursor-not-allowed"
                      checked={selected.has(u.id)}
                      onChange={() => toggleOne(u.id)}
                      disabled={isSelf}
                      aria-label={`Select ${u.username}`}
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <p className="font-medium text-gray-900 dark:text-gray-100">{u.username}</p>
                        {isSelf && <Badge variant="muted">You</Badge>}
                      </div>
                    </div>
                    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full text-white ${u.role === 'admin' ? 'bg-indigo-600' : 'bg-gray-500'}`}>{u.role}</span>
                    <div className="flex gap-2">
                      <Button size="sm" variant="outline" aria-label={`Edit ${u.username}`} onClick={() => openEdit(u)}>
                        <Edit2 className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="sm" variant="outline"
                        aria-label={`Reset password for ${u.username}`}
                        title="Reset password"
                        onClick={() => { setResetUser(u); setTempPassword(null) }}
                      >
                        <KeyRound className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="sm" variant="danger"
                        aria-label={`Delete ${u.username}`}
                        onClick={() => setDeleteId(u.id)}
                        disabled={isSelf}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </div>
                )
              })}
            </div>
          </CardContent>
        </Card>
      )}

      <Modal open={showModal} onClose={closeModal}>
        <ModalHeader onClose={closeModal}>{editing ? `Edit ${editing.username}` : 'Add User'}</ModalHeader>
        <ModalBody>
          <div className="space-y-4">
            {!editing && (
              <Input
                label="Username"
                value={form.username}
                onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
                required
              />
            )}
            <Input
              label={editing ? 'New Password (leave blank to keep current)' : 'Password'}
              type="password"
              value={form.password}
              onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
              required={!editing}
            />
            <Input
              label="Email (for self-service password reset)"
              type="email"
              value={form.email}
              onChange={e => setForm(f => ({ ...f, email: e.target.value }))}
            />
            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-medium text-gray-700 dark:text-gray-300">Role</label>
              <select
                value={form.role}
                onChange={e => setForm(f => ({ ...f, role: e.target.value as Role }))}
                className="px-3 py-2 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              >
                <option value="user">user</option>
                <option value="admin">admin</option>
              </select>
            </div>
          </div>
        </ModalBody>
        <ModalFooter>
          <Button variant="ghost" onClick={closeModal}>Cancel</Button>
          <Button onClick={() => saveMutation.mutate()} loading={saveMutation.isPending}>
            {editing ? 'Save Changes' : 'Create User'}
          </Button>
        </ModalFooter>
      </Modal>

      {/* v5.22.7 option A — admin reset. Two states: confirm, then show-once. */}
      <Modal open={!!resetUser} onClose={() => { setResetUser(null); setTempPassword(null) }}>
        <ModalHeader onClose={() => { setResetUser(null); setTempPassword(null) }}>
          Reset password{resetUser ? ` — ${resetUser.username}` : ''}
        </ModalHeader>
        <ModalBody>
          {tempPassword ? (
            <div className="space-y-3">
              <p className="text-sm text-gray-700 dark:text-gray-300">
                New temporary password. <strong>Copy it now</strong> — it is not stored
                in plaintext and cannot be shown again.
              </p>
              <code className="block select-all break-all rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 px-3 py-2 text-sm font-mono text-gray-900 dark:text-gray-100">
                {tempPassword}
              </code>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Any password-reset link previously emailed to this user has been voided.
              </p>
            </div>
          ) : (
            <p className="text-sm text-gray-700 dark:text-gray-300">
              Generate a new temporary password for{' '}
              <strong>{resetUser?.username}</strong>? Their current password stops
              working immediately.
            </p>
          )}
        </ModalBody>
        <ModalFooter>
          {tempPassword ? (
            <Button onClick={() => { setResetUser(null); setTempPassword(null) }}>Done</Button>
          ) : (
            <>
              <Button variant="ghost" onClick={() => setResetUser(null)}>Cancel</Button>
              <Button
                onClick={() => resetUser && resetMutation.mutate(resetUser.id)}
                loading={resetMutation.isPending}
              >
                Reset password
              </Button>
            </>
          )}
        </ModalFooter>
      </Modal>

      <ConfirmDialog
        open={!!deleteId}
        title="Delete User"
        message="This user will lose all access immediately."
        confirmLabel="Delete"
        variant="danger"
        loading={deleteMutation.isPending}
        onConfirm={() => { deleteMutation.mutate(deleteId!); setDeleteId(null) }}
        onCancel={() => setDeleteId(null)}
      />

      <ConfirmDialog
        open={bulkConfirmOpen}
        title={`Delete ${selectedCount} user${selectedCount === 1 ? '' : 's'}`}
        message={`The selected user${selectedCount === 1 ? '' : 's'} will lose all access immediately. (Self never included. Last admin is protected.)`}
        confirmLabel={`Delete ${selectedCount}`}
        variant="danger"
        loading={bulkDeleteMutation.isPending}
        onConfirm={() => bulkDeleteMutation.mutate(Array.from(selected))}
        onCancel={() => setBulkConfirmOpen(false)}
      />
    </div>
  )
}
