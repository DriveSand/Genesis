import type {
  AdvancedQuery,
  ContentsResponse,
  ImageMetadata,
  ModelsResponse,
  SearchResponse,
  Settings,
} from './types'

export class ApiError extends Error {
  code: number
  constructor(code: number, message: string) {
    super(message)
    this.code = code
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init)
  const text = await resp.text()
  let data: any = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = null
    }
  }
  if (!resp.ok) {
    const message = data?.error?.message || resp.statusText || 'Request failed'
    throw new ApiError(Number(data?.error?.code ?? resp.status), message)
  }
  return data as T
}

function jsonInit(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export interface UploadPrepareItem {
  name: string
  mime: string
  sha256: string
  size: number
}

export interface UploadPrepareResult {
  task_id: string
  chunk_size: number
  results: Array<{ name: string; duplicate: boolean; token?: string }>
}

export interface UploadExecuteResult {
  token: string
  name: string
  saved_path: string
  chunk_index?: number
  status: 'received' | 'success' | 'fail'
  error?: string
}

export const api = {
  status: () => request<{
    ok: boolean
    read_only: boolean
    enable_thumbnail: boolean
    version: string
    database_version: number
  }>('/api/v1/status'),

  getSettings: () => request<{ settings: Settings }>('/api/v1/settings'),

  updateSettings: (change: Partial<Settings>) =>
    request<{ ok: boolean; settings: Settings }>('/api/v1/settings/update', jsonInit({ change })),

  thumbnailCache: () =>
    request<{ files: number; bytes: number; short_side: number }>('/api/v1/settings/thumbnail-cache'),

  clearThumbnailCache: () =>
    request<{ ok: boolean }>('/api/v1/settings/thumbnail-cache/clear', jsonInit({})),

  regenerateThumbnails: () =>
    request<{ ok: boolean; files: number }>('/api/v1/settings/regenerate-thumbnails', jsonInit({})),

  contents: (path: string, maxQuantity: number, cursor = '') => {
    const params = new URLSearchParams({
      path,
      max_quantity: String(maxQuantity),
    })
    if (cursor) params.set('cursor', cursor)
    return request<ContentsResponse>(`/api/v1/directories/contents?${params}`)
  },

  metadata: (imagePath: string) =>
    request<ImageMetadata>(`/api/v1/images/metadata?image_path=${encodeURIComponent(imagePath)}`),

  models: (imagePath: string) =>
    request<ModelsResponse>(`/api/v1/images/models?image_path=${encodeURIComponent(imagePath)}`),

  updateMetadata: (target: { name: string; path: string }, change: Record<string, unknown>) =>
    request<{ ok: boolean }>('/api/v1/images/metadata/update', jsonInit({ target, change })),

  reparseMetadata: (imagePath: string) =>
    request<{ ok: boolean; has_metadata: boolean; tool: string }>(
      `/api/v1/images/metadata/refresh?image_path=${encodeURIComponent(imagePath)}`,
      { method: 'POST' },
    ),

  reparseFolderMetadata: (folderPath: string, recursive: boolean) =>
    request<{ ok: boolean; files: number; updated: number; failed: number }>(
      `/api/v1/images/metadata/refresh-folder?folder_path=${encodeURIComponent(folderPath)}`
      + `&recursive=${recursive ? 'true' : 'false'}`,
      { method: 'POST' },
    ),

  modelNames: (kind: string) =>
    request<{ kind: string; names: string[] }>(`/api/v1/images/model-names?kind=${kind}`),

  searchBasic: (content: string[], path: string[], maxQuantity: number, cursor = '') =>
    request<SearchResponse>('/api/v1/search/basic', jsonInit({ content, path, max_quantity: maxQuantity, cursor })),

  searchAdvanced: (query: AdvancedQuery, maxQuantity: number, cursor = '') =>
    request<SearchResponse>('/api/v1/search/advanced', jsonInit({ ...query, max_quantity: maxQuantity, cursor })),

  deleteEntries: (content: Array<{ type: string; name: string; path: string }>) =>
    request<{ results: Array<{ name: string; path: string; ok: boolean; error?: string }> }>(
      '/api/v1/files/delete', jsonInit({ content })),

  moveEntries: (content: Array<{ type: string; name: string; source_path: string; target_path: string }>) =>
    request<{ results: Array<{ name: string; path: string; ok: boolean; new_path?: string; error?: string }> }>(
      '/api/v1/files/move', jsonInit({ content })),

  copyEntries: (content: Array<{ type: string; name: string; source_path: string; target_path: string }>) =>
    request<{ results: Array<{ name: string; path: string; ok: boolean; new_path?: string; error?: string }> }>(
      '/api/v1/files/copy', jsonInit({ content })),

  rename: (type: string, name: string, path: string, newName: string) =>
    request<{ ok: boolean }>('/api/v1/files/rename', jsonInit({ type, name, path, new_name: newName })),

  createFolder: (path: string, name: string, autoRename = false) =>
    request<{ ok: boolean; name: string; new_path: string }>(
      '/api/v1/files/folder', jsonInit({ path, name, auto_rename: autoRename })),

  checkFolders: (target: string, paths: string[]) =>
    request<{ existing: string[] }>('/api/v1/uploads/check-folders',
      jsonInit({ target, paths })),

  prepareUpload: (target: string, files: UploadPrepareItem[]) =>
    request<UploadPrepareResult>('/api/v1/uploads/prepare', jsonInit({ target, files })),

  executeUpload: (form: FormData) =>
    request<UploadExecuteResult>('/api/v1/uploads/execute', { method: 'POST', body: form }),

  thumbnailUrl: (sha256: string) =>
    `/api/v1/images/thumbnail?image_sha256=${encodeURIComponent(sha256)}`,

  regenerateThumbnail: (sha256: string) =>
    request<{ ok: boolean; submitted: boolean }>(
      `/api/v1/images/thumbnail/regenerate?image_sha256=${encodeURIComponent(sha256)}`,
      { method: 'POST' },
    ),

  originalUrl: (imagePath: string) =>
    `/api/v1/images/original?image_path=${encodeURIComponent(imagePath)}`,

  downloadSingleUrl: (name: string, path: string) =>
    `/api/v1/downloads/single?name=${encodeURIComponent(name)}&path=${encodeURIComponent(path)}`,

  downloadBatch: async (body: {
    folders: Array<{ name: string; path: string }>
    files: Array<{ name: string; path: string }>
  }): Promise<Blob> => {
    const resp = await fetch('/api/v1/downloads/batch', jsonInit(body))
    if (!resp.ok) {
      let message = resp.statusText
      try {
        const data = await resp.json()
        message = data?.error?.message || message
      } catch {
        /* ignore */
      }
      throw new ApiError(resp.status, message || 'Download failed')
    }
    return resp.blob()
  },
}

export function imagePathOf(file: { path: string; name: string }): string {
  return file.path === '/' ? `/${file.name}` : `${file.path}/${file.name}`
}

export function formatBytes(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return unit === 0 ? `${bytes} B` : `${value.toFixed(2)} ${units[unit]}`
}

export function formatTime(seconds: number): string {
  if (!seconds) return '—'
  const d = new Date(seconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  const now = new Date()
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  const date = `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`
  return sameDay ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function sha256OfFile(file: File, onProgress?: (ratio: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(reader.error ?? new Error('Failed to read file'))
    reader.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total)
    }
    reader.onload = async () => {
      try {
        const buffer = reader.result as ArrayBuffer
        const digest = await crypto.subtle.digest('SHA-256', buffer)
        const hex = Array.from(new Uint8Array(digest))
          .map((b) => b.toString(16).padStart(2, '0'))
          .join('')
        resolve(hex)
      } catch (err) {
        reject(err)
      }
    }
    reader.readAsArrayBuffer(file)
  })
}
