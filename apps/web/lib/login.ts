export interface LoginRequest {
  name: string;
  password?: string;
  bootstrap_token?: string;
}

export interface SingleUserSetupRequest {
  name: string;
}

export function buildSingleUserSetupRequest(
  name: string,
): SingleUserSetupRequest {
  return { name: name.trim() };
}

export function buildLoginRequest(
  name: string,
  password: string,
  bootstrapToken: string,
): LoginRequest {
  const request: LoginRequest = { name: name.trim() };
  if (password) request.password = password;
  const normalizedBootstrap = bootstrapToken.trim();
  if (normalizedBootstrap) request.bootstrap_token = normalizedBootstrap;
  return request;
}
