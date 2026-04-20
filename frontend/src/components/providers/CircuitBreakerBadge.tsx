import { Badge } from '@/components/ui/Badge'

interface Props {
  state: 'closed' | 'open' | 'half-open'
}

export function CircuitBreakerBadge({ state }: Props) {
  if (state === 'closed') return <Badge variant="success">Online</Badge>
  if (state === 'open') return <Badge variant="danger">Tripped</Badge>
  return <Badge variant="warning">Recovering</Badge>
}
