import { Badge } from '@/components/ui/Badge'

interface Props {
  state: 'closed' | 'open' | 'half-open'
}

export function CircuitBreakerBadge({ state }: Props) {
  if (state === 'closed') return <Badge variant="success">Closed</Badge>
  if (state === 'open') return <Badge variant="danger">Open</Badge>
  return <Badge variant="warning">Half-Open</Badge>
}
