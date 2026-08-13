const BASE_TOKEN_COOKIE = "sag_token";

export function tokenCookieName(host: string): string {
  const candidate = host.split(",", 1)[0]?.trim();
  if (!candidate) return BASE_TOKEN_COOKIE;
  try {
    const port = new URL(`http://${candidate}`).port;
    return port ? `${BASE_TOKEN_COOKIE}_${port}` : BASE_TOKEN_COOKIE;
  } catch {
    return BASE_TOKEN_COOKIE;
  }
}
