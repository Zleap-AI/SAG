/** 浏览器通过局域网 IP 打开前端时，自动将 API 指向同主机 8000 端口。 */
export function resolveApiBase(configured?: string): string {
  if (typeof window !== "undefined") {
    if (window.sagDesktop?.isDesktop && window.sagDesktop.apiBase) {
      return window.sagDesktop.apiBase;
    }
    const { protocol, hostname } = window.location;
    const isLocalHost = hostname === "localhost" || hostname === "127.0.0.1";
    if (!isLocalHost) {
      const pointsToLocal =
        !configured ||
        configured.includes("localhost") ||
        configured.includes("127.0.0.1");
      if (pointsToLocal) {
        return `${protocol}//${hostname}:8000`;
      }
    }
  }
  return configured || "http://localhost:8000";
}
