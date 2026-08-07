export function formatBytes(bytes: number): string {
  const gb = bytes / (1024 * 1024 * 1024);
  return `${gb.toFixed(1)} GB`;
}

export function progressPercent(bytesCopied: number, totalBytes: number): number {
  if (totalBytes === 0) return 0;
  return Math.round((bytesCopied / totalBytes) * 100);
}
