export interface Food {
  id: number;
  name: string;
  category: string;
  production_date: string; // YYYY-MM-DD
  shelf_life_days: number;
  quantity: number;
  expiry_date: string;
  days_remaining: number;
  device_type: number; // 0=食品 1=饮品
  created_at: string;
  updated_at: string;
  consumed_at: string | null;
}

export interface FoodInput {
  name: string;
  category: string;
  production_date: string;
  shelf_life_days: number;
  quantity: number;
}

export interface Stats {
  total_active: number;
  expired: number;
  expiring_3d: number;
  expiring_7d: number;
  total_consumed: number;
  by_category: { category: string; count: number }[];
}

export interface PushStatus {
  ok: boolean | null;
  reason: string;
  error: string | null;
  device: string | null;
  items: string[];
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  in_progress: boolean;
}

export interface PreviewItem {
  slot: number;
  name: string;
  category: string;
  device_type: number;
  expiry_date: string;
  days_remaining: number;
  bitmap_ok: boolean;
}

export interface Meta {
  categories: string[];
  font_available: boolean;
  font_path: string | null;
}

export interface Health {
  ok: boolean;
  db: boolean;
  error: string | null;
}

export interface OtaPackage {
  id: number;
  filename: string;
  app_version: number | null;
  device_type: number | null;
  softdevice_required: string | null;
  size: number;
  sha256: string;
  uploaded_at: string | null;
}

export interface OtaStatus {
  ok: boolean | null;
  firmware: string | null;
  firmware_id: number | null;
  app_version: number | null;
  device: string | null;
  sent_bytes: number;
  total_bytes: number | null;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  error: string | null;
  in_progress: boolean;
}

export interface DeviceVersion {
  device: string;
  version: number;
  version_hex: string;
}
