import packageMetadata from "@/package.json";

export const APP_VERSION = packageMetadata.version;

export function AppVersionBadge() {
  return (
    <span
      aria-label={`SAG version ${APP_VERSION}`}
      className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium leading-none text-muted-foreground"
    >
      v{APP_VERSION}
    </span>
  );
}
