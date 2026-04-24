import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Edit2 } from 'lucide-react'
import { usersApi } from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
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
  const [form, setForm] = useState({ username: '', password: '', role: 'user' as Role })
  const [deleteId, setDeleteId] = useState<string | null>(null)

  const { data: users, isLoading } = useQuery({ queryKey: ['users'], queryFn: usersApi.list })

  const saveMutation = useMutation({
    mutationFn: () => editing
      ? usersApi.update(editing.id, { password: form.password || undefined, role: form.role })
      : usersApi.create({ username: form.username, password: form.password, role: form.role }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] })
      toast.success(editing ? 'User updated' : 'User created')
      closeModal()
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => usersApi.delete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['users'] }); toast.success('User deleted') },
    onError: (e: Error) => toast.error(e.message),
  })

  function openCreate() {
    setEditing(null)
    setForm({ username: '', password: '', role: 'user' })
    setShowModal(true)
  }

  function openEdit(u: User) {
    setEditing(u)
    setForm({ username: u.username, password: '', role: u.role as Role })
    setShowModal(true)
  }

  function closeModal() { setShowModal(false); setEditing(null) }

  return (
    <div className="p-6 space-y-6 max-w-4xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Users</h1>
          <p className="text-sm text-gray-500 mt-0.5">{users?.length ?? 0} users</p>
        </div>
        <Button size="sm" onClick={openCreate}><Plus className="h-4 w-4 mr-1.5" />Add User</Button>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner /></div>
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {(users ?? []).map(u => (
                <div key={u.id} className="flex items-center gap-3 px-5 py-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="font-medium text-gray-900 dark:text-gray-100">{u.username}</p>
                      {u.username === me?.username && <Badge variant="muted">You</Badge>}
                    </div>
                  </div>
                  <span className={`text-xs font-semibold px-2 py-0.5 rounded-full text-white ${u.role === 'admin' ? 'bg-indigo-600' : 'bg-gray-500'}`}>{u.role}</span>
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" onClick={() => openEdit(u)}>
                      <Edit2 className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      size="sm" variant="danger"
                      onClick={() => setDeleteId(u.id)}
                      disabled={u.username === me?.username}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              ))}
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

      <ConfirmDialog
        open={!!deleteId}
        title="Delete User"
        message="This user will lose all access immediately."
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => { deleteMutation.mutate(deleteId!); setDeleteId(null) }}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  )
}
