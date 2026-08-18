// Single place where the API base URL is resolved. Defaults to the relative
// `/api` prefix, which the Vite dev server proxies to the FastAPI backend.
const BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'

export async function apiGet(path, params) {
  const query = params ? `?${new URLSearchParams(params)}` : ''
  const response = await fetch(`${BASE_URL}${path}${query}`)

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // Non-JSON error body: keep the status-based message.
    }
    throw new Error(detail)
  }

  return response.json()
}

export const getHealth = () => apiGet('/health')
