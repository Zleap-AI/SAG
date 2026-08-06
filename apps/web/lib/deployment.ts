/** Browser-visible base path for a normal or fnOS-gateway deployment. */
function normalizeBasePath(value: string | undefined): string {
  if (!value || value === "/") return "";
  const withLeadingSlash = value.startsWith("/") ? value : `/${value}`;
  return withLeadingSlash.replace(/\/+$/, "");
}

export const APP_BASE_PATH = normalizeBasePath(process.env.NEXT_PUBLIC_APP_BASE_PATH);

/** Prefix an application URL once, while preserving normal root deployments. */
export function appPath(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (
    !APP_BASE_PATH ||
    normalizedPath === APP_BASE_PATH ||
    normalizedPath.startsWith(`${APP_BASE_PATH}/`)
  ) {
    return normalizedPath;
  }
  return `${APP_BASE_PATH}${normalizedPath}`;
}
