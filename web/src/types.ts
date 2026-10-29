export interface Settings {
  EnableThumbnail: boolean
  ThumbnailShortSideSize: number
  NewlyAddedImageLevel: number
  GridCellSize: number
  AutoRefresh: boolean
  ShowLevel: number[]
  SafeBrowsingMode: boolean
  BlurLevel: number
  DatabaseIdleCheckpointMinutes: number
}

export interface FolderEntry {
  name: string
  uuid: string
  created_time: number
  edit_time: number
  path: string
  scanned_path: boolean
}

export interface FileEntry {
  name: string
  sha256: string
  rating: number
  file_size: number
  add_time: number
  path: string
}

export interface ContentsResponse {
  folders: FolderEntry[]
  files: FileEntry[]
  current_cursor: string
  uuid: string
}

export interface ModelSimple {
  model_name: string
  version: string
  model_url: string
}

export interface ModelLora extends ModelSimple {
  weight: number
  type: number
}

export interface ModelsResponse {
  checkpoint: ModelSimple[]
  lora: ModelLora[]
  embedding: ModelSimple[]
  vae: ModelSimple[]
  upscale: ModelSimple[]
}

export interface ImageMetadata {
  positive_prompt: string
  negative_prompt: string
  seed: number
  steps: number
  cfg: number
  sampler: string
  scheduler: string
  tool: string
  rating: number
  note: string
  width: number
  height: number
  file_size: number
  file_name: string
  added_at: number
  sha256: string
  mime_type: string
}

export interface SearchFile {
  name: string
  sha256: string
  rating: number
  file_size: number
  add_time: number
  path: string
}

export interface SearchResponse {
  files: SearchFile[]
  current_cursor: string
}

export interface ModelRule {
  name: string
  ver: string
}

export interface IncludeExclude {
  include: string[]
  exclusion: string[]
}

export interface RangeRule {
  bigger_than: number
  smaller_than: number
}

export interface TimeRule {
  after: number
  before: number
}

export interface AdvancedQuery {
  path: IncludeExclude
  file_name: IncludeExclude
  file_type: string[]
  file_size: RangeRule[]
  width: RangeRule[]
  height: RangeRule[]
  sha256: string[]
  prompt: IncludeExclude
  neg_prompt: IncludeExclude
  ckpt: ModelRule[]
  lora: ModelRule[]
  embed: ModelRule[]
  tool: string[]
  seed: number[]
  sampler: string[]
  scheduler: string[]
  add_time: TimeRule[]
  rating: number[]
  note: string[]
}

export const RATING_NAMES = ['G', 'PG-13', 'R', 'X', 'XXX'] as const

// 开始模糊的等级: -1 表示不模糊任何等级 (None)
export const BLUR_LEVELS = [
  { value: -1, label: 'None' },
  { value: 1, label: 'PG-13' },
  { value: 2, label: 'R' },
  { value: 3, label: 'X' },
  { value: 4, label: 'XXX' },
] as const

export const RATING_COLORS = [
  '#0078d4', // G: 蓝
  '#c99a00', // PG-13: 黄
  '#d13438', // R: 红
  '#d13438', // X: 红
  '#d13438', // XXX: 红
] as const

// 统一后的最终记录名
export const SAMPLERS = [
  'Euler', 'Euler ancestral', 'DPMPP 2M', 'DPMPP 2M SDE', 'DDIM',
  'UniPC', 'LCM', 'DDPM', 'Heun', 'LMS', 'DPM Fast',
  'Euler CFG PP', 'Euler ancestral CFG PP', 'ER SDE',
]
export const SCHEDULERS = [
  'Simple', 'Karras', 'Normal', 'Exponential', 'SGM Uniform',
  'DDIM Uniform', 'Beta', 'KL Optimal', 'Linear Quadratic',
]
export const TOOLS = ['ComfyUI', 'A1111 WebUI', 'InvokeAI', 'NovelAI', 'Fooocus', 'SD.Next']
export const FILE_TYPES = ['JPEG', 'PNG', 'WEBP', 'GIF', 'BMP', 'TIFF', 'MP4', 'WEBM']
export const MODEL_KINDS = ['Checkpoint', 'LoRA', 'LyCORIS', 'Embedding', 'VAE'] as const
