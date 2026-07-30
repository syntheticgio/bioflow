import refIconPng from "./ref.png";

export function ReferenceIcon({ className = "" }: { className?: string }) {
  return (
    <img
      src={refIconPng}
      alt="Reference"
      className={className}
      width="32"
      height="32"
      style={{ display: "block" }}
    />
  );
}
