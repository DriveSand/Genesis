import { computed, reactive } from 'vue'
import { api, ApiError } from './api'
import type {
  AdvancedQuery,
  FileEntry,
  FolderEntry,
  ImageMetadata,
  ModelsResponse,
  SearchFile,
  Settings,
} from './types'

export interface ToastItem {
  id: number
  text: string
  kind: 'info' | 'success' | 'error'
}

export interface ContextTarget {
  kind: 'file' | 'folder'
  name: string
  path: string
  sha256?: string
}

const DEFAULT_SETTINGS: Settings = {
  EnableThumbnail: true,
  ThumbnailShortSideSize: 512,
  NewlyAddedImageLevel: 1,
  GridCellSize: 240,
  AutoRefresh: true,
  ShowLevel: [0, 1, 2, 3, 4],
  SafeBrowsingMode: false,
  BlurLevel: 2,
  DatabaseIdleCheckpointMinutes: 10,
}

type SearchParams =
  | { kind: 'basic'; content: string[]; paths: string[] }
  | { kind: 'advanced'; query: AdvancedQuery }

export const store = reactive({
  ready: false,
  standalone: false,
  readOnly: false,
  settings: { ...DEFAULT_SETTINGS },
  path: '/',
  currentUuid: '',
  currentName: '根',
  breadcrumbs: [{ name: '根', path: '/' }] as Array<{ name: string; path: string }>,
  folders: [] as FolderEntry[],
  files: [] as FileEntry[],
  cursor: '',
  hasMore: false,
  loading: false,
  loadingMore: false,
  inSearch: false,
  searchFiles: [] as SearchFile[],
  searchCursor: '',
  searchHasMore: false,
  searchSummary: '',
  quickOpen: false,
  searchPanelOpen: false,
  settingsOpen: false,
  uploadOpen: false,
  viewerOpen: false,
  viewerIndex: 0,
  viewerLoading: false,
  metadata: null as ImageMetadata | null,
  models: null as ModelsResponse | null,
  viewerBust: Date.now(),
  editOpen: false,
  editFile: null as SearchFile | null,
  moveOpen: false,
  moveTargets: [] as ContextTarget[],
  toasts: [] as ToastItem[],
  context: { open: false, x: 0, y: 0, target: null as ContextTarget | null },
  confirm: { open: false, title: '', message: '', danger: false },
  prompt: { open: false, title: '', label: '', value: '' },
})

let pendingConfirm: ((value: boolean) => void) | null = null
let pendingPrompt: ((value: string | null) => void) | null = null
let toastSeq = 0
let scanner: EventSource | null = null
let refreshTimer: number | undefined
let lastSearch: SearchParams | null = null
let lastBasicText = ''

// ---------------------------------------------------------------------- #
// 展示与过滤
// ---------------------------------------------------------------------- #

function ratingAllowed(rating: number): boolean {
  if (store.settings.SafeBrowsingMode) return rating === 0
  return store.settings.ShowLevel.includes(rating)
}

export function isBlurred(rating: number): boolean {
  return !store.settings.SafeBrowsingMode && rating >= store.settings.BlurLevel
}

export const visibleFiles = computed<SearchFile[]>(() => {
  const source: SearchFile[] = store.inSearch
    ? store.searchFiles
    : store.files.map((f) => ({
        name: f.name,
        sha256: f.sha256,
        rating: f.rating,
        file_size: f.file_size,
        add_time: f.add_time,
        path: f.path,
      }))
  // 独立查看页 (新标签页) 不受分级过滤影响
  if (store.standalone) return source
  return source.filter((f) => ratingAllowed(f.rating))
})

export const visibleFolders = computed<FolderEntry[]>(() =>
  store.inSearch ? [] : store.folders,
)

export const currentViewerFile = computed<SearchFile | null>(
  () => visibleFiles.value[store.viewerIndex] ?? null,
)

export function gridQuantity(): number {
  const cell = Math.max(64, store.settings.GridCellSize)
  const columns = Math.max(1, Math.floor(window.innerWidth / cell))
  const rows = Math.max(1, Math.floor((window.innerHeight * 0.9) / cell))
  return Math.max(10, columns * rows)
}

export function imagePathOf(file: { path: string; name: string }): string {
  return file.path === '/' ? `/${file.name}` : `${file.path}/${file.name}`
}

// ---------------------------------------------------------------------- #
// 消息 / 确认 / 输入
// ---------------------------------------------------------------------- #

export function toast(text: string, kind: ToastItem['kind'] = 'info'): void {
  const id = ++toastSeq
  store.toasts.push({ id, text, kind })
  window.setTimeout(() => {
    const index = store.toasts.findIndex((t) => t.id === id)
    if (index >= 0) store.toasts.splice(index, 1)
  }, 4200)
}

export function toastError(err: unknown, prefix = ''): void {
  const message = err instanceof ApiError ? err.message : String(err)
  toast(prefix ? `${prefix}: ${message}` : message, 'error')
}

/** 复制文本到剪贴板 (带结果提示); 空文本给出提示而不写入剪贴板。 */
export async function copyTextToClipboard(text: string,
                                          label: string): Promise<void> {
  if (!text) {
    toast(`${label}为空`, 'info')
    return
  }
  try {
    await navigator.clipboard.writeText(text)
    toast(`已复制${label}`, 'success')
  } catch {
    const area = document.createElement('textarea')
    area.value = text
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    area.remove()
    toast(`已复制${label}`, 'success')
  }
}

export function askConfirm(title: string, message: string,
                           options: { danger?: boolean } = {}): Promise<boolean> {
  store.confirm = { open: true, title, message,
                    danger: Boolean(options.danger) }
  return new Promise((resolve) => {
    pendingConfirm = resolve
  })
}

export function resolveConfirm(value: boolean): void {
  store.confirm.open = false
  pendingConfirm?.(value)
  pendingConfirm = null
}

export function askText(title: string, label: string, value = ''): Promise<string | null> {
  store.prompt = { open: true, title, label, value }
  return new Promise((resolve) => {
    pendingPrompt = resolve
  })
}

export function resolvePrompt(value: string | null): void {
  store.prompt.open = false
  pendingPrompt?.(value)
  pendingPrompt = null
}

// ---------------------------------------------------------------------- #
// 初始化
// ---------------------------------------------------------------------- #

export async function init(): Promise<void> {
  try {
    const status = await api.status()
    store.readOnly = status.read_only
    const { settings } = await api.getSettings()
    store.settings = { ...DEFAULT_SETTINGS, ...settings }
  } catch (err) {
    toastError(err, '初始化失败')
  }
  await loadContents('/', false)
  store.ready = true
}

export async function loadSettings(): Promise<void> {
  const { settings } = await api.getSettings()
  store.settings = { ...DEFAULT_SETTINGS, ...settings }
  syncScanner()
}

// 设置同步: 本地立即生效, 1 秒内合并多次修改后再写回后端,
// 避免快速调整时频繁触发配置文件写入 (写失败概率也随之降低)
let pendingSettings: Partial<Settings> = {}
let settingsTimer: number | undefined
let settingsInFlight = false

function scheduleSettingsPush(): void {
  if (settingsTimer) window.clearTimeout(settingsTimer)
  settingsTimer = window.setTimeout(() => {
    void pushSettings()
  }, 1000)
}

async function pushSettings(): Promise<void> {
  settingsTimer = undefined
  if (settingsInFlight || !Object.keys(pendingSettings).length) return
  const change = pendingSettings
  pendingSettings = {}
  settingsInFlight = true
  try {
    const { settings } = await api.updateSettings(change)
    store.settings = { ...DEFAULT_SETTINGS, ...settings }
    syncScanner()
  } catch (err) {
    toastError(err, '保存设置失败')
    await loadSettings()
  } finally {
    settingsInFlight = false
    if (Object.keys(pendingSettings).length) scheduleSettingsPush()
  }
}

export function applySettings(change: Partial<Settings>): void {
  store.settings = { ...store.settings, ...change }
  pendingSettings = { ...pendingSettings, ...change }
  if ('AutoRefresh' in change) syncScanner()
  scheduleSettingsPush()
}

// ---------------------------------------------------------------------- #
// 目录浏览
// ---------------------------------------------------------------------- #

function buildBreadcrumbs(path: string): Array<{ name: string; path: string }> {
  const crumbs = [{ name: '根', path: '/' }]
  if (path && path !== '/') {
    let current = ''
    for (const part of path.split('/').filter(Boolean)) {
      current += `/${part}`
      crumbs.push({ name: part, path: current })
    }
  }
  return crumbs
}

export async function loadContents(path: string, append = false): Promise<void> {
  if (store.loading || store.loadingMore) return
  if (append && !store.hasMore) return
  if (append) store.loadingMore = true
  else store.loading = true
  try {
    const data = await api.contents(path, gridQuantity(), append ? store.cursor : '')
    if (!append) {
      store.folders = data.folders
      store.files = data.files
      store.path = path
      store.currentUuid = data.uuid
      store.currentName = path === '/' ? '根' : path.split('/').filter(Boolean).pop() ?? '根'
      store.breadcrumbs = buildBreadcrumbs(path)
    } else {
      store.folders.push(...data.folders)
      store.files.push(...data.files)
    }
    store.cursor = data.current_cursor
    store.hasMore = (data.folders.length + data.files.length) > 0 &&
      Boolean(data.current_cursor)
    if (!append) syncScanner()
  } catch (err) {
    toastError(err, '读取目录失败')
  } finally {
    store.loading = false
    store.loadingMore = false
  }
}

export async function navigate(path: string): Promise<void> {
  store.inSearch = false
  store.searchFiles = []
  store.path = path
  await loadContents(path, false)
}

export function goUp(): void {
  if (store.inSearch) {
    exitSearch()
    return
  }
  if (store.path === '/') return
  const parts = store.path.split('/').filter(Boolean)
  parts.pop()
  void navigate(parts.length ? `/${parts.join('/')}` : '/')
}

export async function refresh(): Promise<void> {
  if (store.inSearch) {
    await rerunSearch()
  } else {
    await loadContents(store.path, false)
  }
}

export async function loadMore(): Promise<void> {
  if (store.inSearch) {
    if (!store.searchHasMore || store.loadingMore) return
    store.loadingMore = true
    try {
      await fetchSearchPage(true)
    } finally {
      store.loadingMore = false
    }
    return
  }
  await loadContents(store.path, true)
}

// ---------------------------------------------------------------------- #
// 实时刷新 (SSE)
// ---------------------------------------------------------------------- #

export function stopScanner(): void {
  if (scanner) {
    scanner.close()
    scanner = null
  }
  if (refreshTimer) {
    window.clearTimeout(refreshTimer)
    refreshTimer = undefined
  }
}

export function syncScanner(): void {
  stopScanner()
  if (store.inSearch || !store.settings.AutoRefresh || !store.currentUuid) return
  const params = new URLSearchParams({
    name: store.currentName,
    uuid: store.currentUuid,
  })
  try {
    scanner = new EventSource(`/api/v1/directories/scan?${params}`)
  } catch {
    return
  }
  scanner.addEventListener('change', () => {
    if (refreshTimer) window.clearTimeout(refreshTimer)
    refreshTimer = window.setTimeout(() => {
      if (!store.inSearch && !store.viewerOpen) void loadContents(store.path, false)
    }, 500)
  })
}

// ---------------------------------------------------------------------- #
// 搜索
// ---------------------------------------------------------------------- #

function parseBasicText(text: string): { content: string[]; paths: string[] } {
  const content: string[] = []
  const paths: string[] = []
  for (const part of text.trim().split(/\s+/).filter(Boolean)) {
    if (part.startsWith('$p:')) {
      const value = part.slice(3)
      if (value) paths.push(value)
    } else {
      content.push(part)
    }
  }
  return { content, paths }
}

export function exitSearch(): void {
  store.inSearch = false
  store.searchFiles = []
  store.searchSummary = ''
  lastSearch = null
  syncScanner()
}

async function fetchSearchPage(append: boolean): Promise<void> {
  if (!lastSearch) return
  const cursor = append ? store.searchCursor : ''
  const data = lastSearch.kind === 'basic'
    ? await api.searchBasic(lastSearch.content, lastSearch.paths, gridQuantity(), cursor)
    : await api.searchAdvanced(lastSearch.query, gridQuantity(), cursor)
  if (append) store.searchFiles.push(...data.files)
  else store.searchFiles = data.files
  store.searchCursor = data.current_cursor
  store.searchHasMore = data.files.length > 0
}

export async function runBasicSearch(text: string): Promise<void> {
  const parsed = parseBasicText(text)
  lastBasicText = text
  if (!parsed.content.length && !parsed.paths.length) {
    exitSearch()
    return
  }
  store.loading = true
  try {
    lastSearch = { kind: 'basic', content: parsed.content, paths: parsed.paths }
    await fetchSearchPage(false)
    store.inSearch = true
    store.searchSummary = text
    stopScanner()
  } catch (err) {
    toastError(err, '搜索失败')
  } finally {
    store.loading = false
  }
}

export async function runAdvancedSearch(query: AdvancedQuery): Promise<void> {
  store.loading = true
  try {
    lastSearch = { kind: 'advanced', query }
    await fetchSearchPage(false)
    store.inSearch = true
    store.searchSummary = '高级搜索'
    store.searchPanelOpen = false
    stopScanner()
  } catch (err) {
    toastError(err, '搜索失败')
  } finally {
    store.loading = false
  }
}

export async function rerunSearch(): Promise<void> {
  if (!lastSearch) return
  store.loading = true
  try {
    await fetchSearchPage(false)
  } catch (err) {
    toastError(err, '搜索失败')
  } finally {
    store.loading = false
  }
}

export function repeatBasicSearch(): void {
  if (lastBasicText) void runBasicSearch(lastBasicText)
}

// ---------------------------------------------------------------------- #
// 图片查看器
// ---------------------------------------------------------------------- #

export async function openViewer(file: SearchFile): Promise<void> {
  const index = visibleFiles.value.findIndex(
    (f) => f.name === file.name && f.path === file.path && f.sha256 === file.sha256,
  )
  resetRapidNav()
  store.viewerIndex = Math.max(0, index)
  store.viewerOpen = true
  await loadViewerDetails()
}

export async function loadViewerDetails(): Promise<void> {
  const file = currentViewerFile.value
  if (!file) return
  store.viewerLoading = true
  store.metadata = null
  store.models = null
  const path = imagePathOf(file)
  try {
    const [metadata, models] = await Promise.all([
      api.metadata(path),
      api.models(path),
    ])
    // 快速连续切换时, 丢弃已经过期的响应
    const latest = currentViewerFile.value
    if (!latest || imagePathOf(latest) !== path) return
    store.metadata = metadata
    store.models = models
  } catch (err) {
    toastError(err, '读取图片信息失败')
  } finally {
    const latest = currentViewerFile.value
    if (latest && imagePathOf(latest) === path) store.viewerLoading = false
  }
}

// 连续快速切换图片时暂时放大步进 (10 张), 停止后自动恢复为 1
const RAPID_NAV_INTERVAL = 300      // 两次操作间隔小于该值视为连续快速
const RAPID_NAV_RESET = 450         // 停止后恢复单步的时间
const RAPID_NAV_STEP = 10
let navLastAt = 0
let navDirection = 0
let navRapid = false
let navResetTimer: number | undefined

function resetRapidNav(): void {
  navRapid = false
  navLastAt = 0
  navDirection = 0
  if (navResetTimer) {
    window.clearTimeout(navResetTimer)
    navResetTimer = undefined
  }
}

export async function viewerStep(delta: number): Promise<void> {
  const count = visibleFiles.value.length
  if (!count) return
  const now = performance.now()
  // 反向切换不累计加速; 与上次方向相同且间隔足够短时进入快速模式
  if (now - navLastAt <= RAPID_NAV_INTERVAL && delta === navDirection) {
    navRapid = true
  } else {
    navRapid = false
  }
  navLastAt = now
  navDirection = delta
  if (navResetTimer) window.clearTimeout(navResetTimer)
  navResetTimer = window.setTimeout(resetRapidNav, RAPID_NAV_RESET)
  const step = navRapid ? RAPID_NAV_STEP : 1
  store.viewerIndex = (store.viewerIndex + delta * step + count) % count
  await loadViewerDetails()
}

export function closeViewer(): void {
  resetRapidNav()
  store.viewerOpen = false
  store.metadata = null
  store.models = null
  if (store.standalone) {
    // 独立查看页: 直接关闭标签页, 浏览器阻止关闭时返回主页
    window.close()
    window.setTimeout(() => {
      window.location.href = '/'
    }, 200)
    return
  }
  if (store.settings.AutoRefresh && !store.inSearch) syncScanner()
}

export function openInNewTab(file: { path: string; name: string }): void {
  const target = `/view?image=${encodeURIComponent(imagePathOf(file))}`
  window.open(target, '_blank', 'noopener')
}

export const editTarget = computed<SearchFile | null>(
  () => store.editFile ?? currentViewerFile.value,
)

export function openEditor(file?: SearchFile | null): void {
  const target = file ?? currentViewerFile.value
  if (!target) return
  store.editFile = target
  store.editOpen = true
}

export function closeEditor(): void {
  store.editOpen = false
  store.editFile = null
}

export async function afterMetadataSaved(): Promise<void> {
  store.viewerBust = Date.now()
  if (store.viewerOpen) await loadViewerDetails()
  if (!store.inSearch) void loadContents(store.path, false)
}

export async function reparseMetadata(file: { path: string; name: string }): Promise<void> {
  const ok = await askConfirm(
    '重新读取元数据',
    '此操作会重新解析文件元数据，并覆盖数据库中现有的提示词、生成参数与模型信息。确定继续吗？',
    { danger: true },
  )
  if (!ok) return
  try {
    const result = await api.reparseMetadata(imagePathOf(file))
    if (result.has_metadata) {
      toast('元数据已重新读取', 'success')
    } else {
      // 无生成参数时分辨率/文件大小仍会被刷新
      toast('未找到生成元数据，已刷新分辨率与文件大小', 'info')
    }
  } catch (err) {
    toastError(err, '重新读取元数据失败')
  } finally {
    // 无论解析结果如何都刷新界面: 分辨率/文件大小可能已更新
    store.viewerBust = Date.now()
    if (store.viewerOpen) await loadViewerDetails()
    if (!store.inSearch) void loadContents(store.path, false)
  }
}

// ---------------------------------------------------------------------- #
// 独立查看页 (/view?image=...): 新标签页直接展示图片查看界面
// ---------------------------------------------------------------------- #

export async function initStandalone(imagePath: string): Promise<void> {
  store.standalone = true
  const slash = imagePath.lastIndexOf('/')
  const parent = slash > 0 ? imagePath.slice(0, slash) : '/'
  const name = imagePath.slice(slash + 1)
  try {
    const { settings } = await api.getSettings()
    store.settings = { ...DEFAULT_SETTINGS, ...settings }
    const data = await api.contents(parent, 500)
    store.path = parent
    store.currentUuid = data.uuid
    store.currentName = parent === '/' ? '根'
      : parent.split('/').filter(Boolean).pop() ?? '根'
    store.breadcrumbs = buildBreadcrumbs(parent)
    store.folders = []
    store.files = data.files
    store.inSearch = false
    let index = data.files.findIndex((f) => f.name === name)
    if (index < 0) {
      // 目标不在首批结果中: 仅展示该图片
      store.files = [{
        name, sha256: '', rating: 0, file_size: 0, add_time: 0, path: parent,
      }]
      index = 0
    }
    store.viewerIndex = index
    store.viewerOpen = true
    store.ready = true
    await loadViewerDetails()
  } catch (err) {
    toastError(err, '打开图片失败')
  }
}

// ---------------------------------------------------------------------- #
// 文件操作
// ---------------------------------------------------------------------- #

export function openContext(event: MouseEvent, target: ContextTarget): void {
  event.preventDefault()
  store.context = {
    open: true,
    x: Math.min(event.clientX, window.innerWidth - 220),
    y: Math.min(event.clientY, window.innerHeight - 220),
    target,
  }
}

export function openBackgroundContext(event: MouseEvent): void {
  event.preventDefault()
  store.context = {
    open: true,
    x: Math.min(event.clientX, window.innerWidth - 220),
    y: Math.min(event.clientY, window.innerHeight - 220),
    target: null,
  }
}

export function closeContext(): void {
  store.context.open = false
}

export async function deleteTargets(targets: ContextTarget[]): Promise<void> {
  if (!targets.length) return
  const ok = await askConfirm(
    '删除确认',
    `确定删除 ${targets.length} 个项目吗？文件夹将连同其中全部内容一并删除，此操作不可撤销。`,
    { danger: true },
  )
  if (!ok) return
  try {
    const { results } = await api.deleteEntries(
      targets.map((t) => ({ type: t.kind, name: t.name, path: t.path })),
    )
    const failed = results.filter((r) => !r.ok)
    if (failed.length) {
      toast(`${failed.length} 个项目删除失败：${failed[0].error ?? ''}`, 'error')
    } else {
      toast(`已删除 ${results.length} 个项目`, 'success')
    }
    await refresh()
  } catch (err) {
    toastError(err, '删除失败')
  }
}

export async function renameTarget(target: ContextTarget): Promise<void> {
  const value = await askText(
    '重命名',
    target.kind === 'folder' ? '文件夹名称' : '文件名称',
    target.name,
  )
  if (!value || value === target.name) return
  try {
    await api.rename(target.kind, target.name, target.path, value)
    toast('重命名成功', 'success')
    await refresh()
  } catch (err) {
    toastError(err, '重命名失败')
  }
}

export async function createFolderHere(): Promise<void> {
  const name = await askText('新建文件夹', '文件夹名称', '')
  if (!name) return
  try {
    await api.createFolder(store.path, name)
    toast('文件夹已创建', 'success')
    await refresh()
  } catch (err) {
    toastError(err, '创建文件夹失败')
  }
}

export function openMoveDialog(targets: ContextTarget[]): void {
  store.moveTargets = [...targets]
  store.moveOpen = true
}

export async function moveTo(targetPath: string): Promise<void> {
  const items = store.moveTargets
  if (!items.length) return
  try {
    const { results } = await api.moveEntries(
      items.map((t) => ({
        type: t.kind,
        name: t.name,
        source_path: t.path,
        target_path: targetPath,
      })),
    )
    const failed = results.filter((r) => !r.ok)
    if (failed.length) {
      toast(`移动失败：${failed[0].error ?? ''}`, 'error')
    } else {
      toast(`已移动 ${results.length} 个项目`, 'success')
    }
    store.moveOpen = false
    await refresh()
  } catch (err) {
    toastError(err, '移动失败')
  }
}

export async function downloadSingle(target: ContextTarget): Promise<void> {
  const url = api.downloadSingleUrl(target.name, target.path)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = target.name
  anchor.click()
}

export async function downloadTargets(
  files: ContextTarget[],
  folders: ContextTarget[],
): Promise<void> {
  if (!files.length && !folders.length) return
  if (!folders.length && files.length === 1) {
    await downloadSingle(files[0])
    return
  }
  try {
    toast('正在打包下载…')
    const blob = await api.downloadBatch({
      folders: folders.map((f) => ({ name: f.name, path: f.path })),
      files: files.map((f) => ({ name: f.name, path: f.path })),
    })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'genesis_images.zip'
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 10000)
  } catch (err) {
    toastError(err, '下载失败')
  }
}

export async function regenerateThumbnail(file: { sha256: string }): Promise<void> {
  try {
    await api.regenerateThumbnail(file.sha256)
    window.setTimeout(() => {
      store.viewerBust = Date.now()
    }, 800)
    toast('已提交缩略图重新生成', 'info')
  } catch (err) {
    toastError(err, '重新生成失败')
  }
}
