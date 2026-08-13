import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { tokenCookieName } from "@/lib/auth-cookie";

const PUBLIC_PATHS = ["/login"];

export function middleware(req: NextRequest) {
  const token = req.cookies.get(tokenCookieName(req.headers.get("host") ?? ""))?.value;
  const { pathname } = req.nextUrl;

  if (pathname === "/") {
    const url = req.nextUrl.clone();
    url.pathname = token ? "/chat" : "/login";
    return NextResponse.redirect(url);
  }

  const isPublic = PUBLIC_PATHS.some((p) => pathname.startsWith(p));

  if (!token && !isPublic) {
    const url = req.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
  if (token && isPublic) {
    const url = req.nextUrl.clone();
    url.pathname = "/chat";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.).*)"],
};
