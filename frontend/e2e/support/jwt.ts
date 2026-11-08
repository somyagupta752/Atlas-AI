import crypto from "node:crypto";
import { readState } from "./state";

const b64 = (value: object | Buffer) =>
  Buffer.from(value instanceof Buffer ? value : JSON.stringify(value)).toString("base64url");

// A GENUINELY SIGNED access token that has already expired. The server verifies the signature and the expiry for real, so a
// request carrying it exercises the same code path as a token that timed out on its own, without waiting 15 minutes.
export function expiredAccessToken(userId: string): string {
  const now = Math.floor(Date.now() / 1000);
  const header = b64({ alg: "HS256", typ: "JWT" });
  const payload = b64({ sub: userId, typ: "access", iss: "atlas-ai", iat: now - 7200, exp: now - 3600, jti: "e2e-expired" });
  const signature = crypto.createHmac("sha256", readState().auth_secret).update(`${header}.${payload}`).digest("base64url");
  return `${header}.${payload}.${signature}`;
}

// A well-formed token signed with the WRONG key: the server must refuse it just as firmly.
export function forgedAccessToken(userId: string): string {
  const now = Math.floor(Date.now() / 1000);
  const header = b64({ alg: "HS256", typ: "JWT" });
  const payload = b64({ sub: userId, typ: "access", iss: "atlas-ai", iat: now, exp: now + 3600 });
  const signature = crypto.createHmac("sha256", "not-the-servers-key").update(`${header}.${payload}`).digest("base64url");
  return `${header}.${payload}.${signature}`;
}
