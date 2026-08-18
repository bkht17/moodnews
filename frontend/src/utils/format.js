// Small formatting helpers shared by the grid and the comparison view.

// Backend timestamps are ISO-8601 UTC strings; render them in the reader's
// own locale rather than shipping a date library for one function.
export function formatDate(iso) {
  if (!iso) return 'Date unknown'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return 'Date unknown'
  return date.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

export function formatRelative(iso) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''

  const minutes = Math.round((Date.now() - date.getTime()) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  return days === 1 ? 'yesterday' : `${days}d ago`
}
