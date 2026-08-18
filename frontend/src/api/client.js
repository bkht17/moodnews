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

// The mood switcher's options. Served by the backend rather than hard-coded
// here, so adding a mood stays a backend-only change.
export const getMoods = () => apiGet('/moods')

// The grid. Returns { items, total, limit, offset }.
export const getNews = (params) => apiGet('/news', params)

// One article. Without a mood the backend returns the original only; with one
// it also returns the rewrite, generating it on demand when it is not cached -
// which can take several seconds.
export const getArticle = (id, mood) =>
  apiGet(`/news/${id}`, mood ? { mood } : undefined)
