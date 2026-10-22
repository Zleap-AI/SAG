import { createServer } from "node:net";

export async function isPortAvailable(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
}

export async function resolveApiPort(host: string, preferredPort: number, webPort: number): Promise<number> {
  if (preferredPort !== webPort && await isPortAvailable(host, preferredPort)) {
    return preferredPort;
  }
  // Let the OS select an available port; keep the persisted browser origin intact.
  for (let attempt = 0; attempt < 3; attempt++) {
    const port = await new Promise<number>((resolve, reject) => {
      const server = createServer();
      server.unref();
      server.once("error", reject);
      server.listen({ host, port: 0, exclusive: true }, () => {
        const address = server.address();
        const selected = address && typeof address !== "string" ? address.port : 0;
        server.close(error => error ? reject(error) : resolve(selected));
      });
    });
    if (port && port !== webPort) return port;
  }
  throw new Error("无法为本机 API 分配空闲端口，请重试 SAG 并检查日志。");
}
