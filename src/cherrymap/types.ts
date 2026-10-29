export type Mode = 'explore' | 'directions' | 'saved' | 'source' | 'uplinks';
export type MapStyle = 'roadmap' | 'satellite' | 'terrain';

export interface GeographicBounds {
  south: number;
  north: number;
  west: number;
  east: number;
}

export interface Place {
  id: string;
  name: string;
  lat: number;
  lng: number;
  origin: 'source' | 'provider' | 'provider-worker' | 'coordinates' | 'open-index' | 'index-cluster' | 'open-poi' | 'neural-feed';
  address?: string;
  source_line?: number;
  place_id?: string;
  category?: string;
  position_origin?: 'source-static' | 'local-update' | 'provider-nearby-worker';
  observed_at?: string | null;
  refreshed_at?: string;
  data_modified_at?: string;
  population?: number;
  country_code?: string;
  overview?: boolean;
  queried_at?: string;
  website?: string;
  opening_hours?: string;
  brand?: string;
  cluster?: { count: number; bounds: GeographicBounds };
  marker?: {
    title: string;
    label: string;
    color: string;
    popup: { heading: string; provenance: string; source_line?: number };
  };
}

export interface NearbyState {
  status: 'zoom' | 'hidden' | 'loading' | 'ready' | 'error';
  places: Place[];
  hasMore: boolean;
  message?: string;
  refreshedAt?: string | null;
  refreshSeconds?: number;
  workerRunning?: boolean;
  coverage?: { radius_meters: number | null; result_limit: number; exhaustive: false };
}

export interface NearbySnapshot {
  status: 'hidden' | 'loading' | 'ready' | 'error';
  places: Place[];
  has_more: boolean;
  last_refresh_at: string | null;
  refresh_seconds: number;
  worker_running: boolean;
  coverage: { radius_meters: number | null; result_limit: number; exhaustive: false };
  message?: string;
}

export interface GeographicDataset {
  name: string;
  record_count: number;
  imported_at: string | null;
  source_url: string;
  license: string;
  version: string;
}

export interface POISnapshot {
  status: 'zoom' | 'hidden' | 'loading' | 'ready' | 'error';
  places: Place[];
  total_in_view: number;
  stored_count: number;
  loaded_count: number;
  has_more: boolean;
  queried_at: string | null;
  refresh_seconds: number;
  message?: string;
  coverage: { source: string; bounded: boolean; exhaustive: false; bounds?: GeographicBounds };
}

export interface IndexSnapshot {
  status: 'hidden' | 'loading' | 'ready' | 'error';
  places: Place[];
  dataset: GeographicDataset;
  worker_running: boolean;
  refreshed_at: string | null;
  total_in_view: number;
  loaded_count: number;
  has_more: boolean;
  mode: 'overview' | 'clusters' | 'places';
  coverage?: {
    represented_records: number;
    named_total: number;
    named_loaded: number;
    cluster_count: number;
    min_population: number;
    includes_capitals: boolean;
  };
  message?: string;
}

export interface SourceStatus {
  status: string;
  filename: string;
  sha256: string;
  python_version: string;
  executed_at: string;
  duration_ms: number;
  exit_code: number;
  report_count: number;
  main_guards_disabled: number;
  location_count: number;
  network_access: boolean;
}

export interface SourceRunnerIdentity {
  kind: 'cherrypro-source-extension';
  filename: string;
  extension_filename: string;
  pid: number;
  started_at: string;
  sha256: string;
  original_sha256: string;
  extension_sha256: string;
}

export interface MarkerFusion {
  markers: Place[];
  fused_at: string;
  counts: { source: number; index: number; provider: number; poi?: number; total: number };
  policy: string;
  data_mode?: string;
}

export interface SoftwareSyncStatus {
  scope: 'software-marker-pipeline';
  status: 'verified' | 'waiting' | 'degraded';
  verified: boolean;
  checked_at: string;
  checks: Record<string, boolean | null>;
  failed_checks: string[];
  maximum_age_seconds: { source: number; index: number; provider: number };
  hardware_verified: false;
  sensor_verified: false;
  coverage: string;
}

export interface MarkerWorkerStatus {
  running: boolean;
  status: 'running' | 'error' | 'stopped';
  interval_seconds: number;
  cycle_count: number;
  revision: number;
  last_refresh_at: string | null;
  last_changed_at: string | null;
  coordinates_mode: 'source-static' | 'local-updates';
  error: string | null;
  changed_count: number;
  runner?: SourceRunnerIdentity;
  sensor_data?: string;
  tpm_status?: string;
  sync?: SoftwareSyncStatus;
}

export interface MarkerSnapshot {
  locations: Place[];
  worker: MarkerWorkerStatus;
  nearby: NearbySnapshot;
  index: IndexSnapshot;
  poi?: POISnapshot;
  fusion?: MarkerFusion;
}

export interface Bootstrap {
  maps: { api_base: string; browser_key: string };
  source: SourceStatus;
  locations: Place[];
  worker?: MarkerWorkerStatus;
  index?: IndexSnapshot;
  poi?: POISnapshot;
  fusion?: MarkerFusion;
  uplinks: { connected: boolean; status: string };
}

export interface RouteData {
  distance: string;
  duration: string;
  points: { lat: number; lng: number }[];
  steps: { instruction: string; distance: string; duration: string }[];
}

export interface ViewState {
  lat: number;
  lng: number;
  zoom: number;
  visibleIds: string[];
  radius?: number;
  bounds?: GeographicBounds;
}

export interface MapActions {
  flyTo: (place: Place) => void;
  zoom: (delta: number) => void;
  tilt: (delta: number) => void;
  heading: (delta: number) => void;
  level: () => void;
  world: () => void;
  fitSource: () => void;
  restore: (lat: number, lng: number, zoom: number) => void;
}
