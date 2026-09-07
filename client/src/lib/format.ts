/** 展示格式化工具 */

export function humanShelfLife(days: number): string {
  if (days > 0 && days % 365 === 0) return `${days / 365} 年`;
  if (days > 0 && days % 30 === 0) return `${days / 30} 个月`;
  return `${days} 天`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(0, 10);
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export const DEVICE_TYPE_LABEL = ["食品", "饮品"] as const;
