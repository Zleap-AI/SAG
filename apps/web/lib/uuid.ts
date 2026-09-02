export class UuidCapabilityError extends Error {
  constructor() {
    super("当前浏览器不支持安全随机 ID；请升级浏览器后重试。");
    this.name = "UuidCapabilityError";
  }
}

/** Generate an RFC 4122 UUID v4 without depending on crypto.randomUUID. */
export function generateUuidV4(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") {
    return cryptoApi.randomUUID();
  }
  if (typeof cryptoApi?.getRandomValues !== "function") {
    throw new UuidCapabilityError();
  }

  const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
