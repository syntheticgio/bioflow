import { getFileIcon } from "./getFileIcon";

interface FileIconProps {
  formatKind: string;
  role?: string | null;
  className?: string;
}

export function FileIcon({
  formatKind,
  role,
  className = "",
}: FileIconProps) {
  const iconPath = getFileIcon(formatKind, role);

  return (
    <img
      src={iconPath}
      alt={`${formatKind} file icon`}
      className={className}
      width="32"
      height="32"
      style={{ display: "block" }}
    />
  );
}
