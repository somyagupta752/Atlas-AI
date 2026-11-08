// Reads the emails the API sent, from the local mail-capture service (frontend/e2e/support/mail_server.py). The API sends real
// SMTP to it, exactly as it would to a provider; it logs no token, so the capture service is the ONLY place a test can get one.
const MAIL_API = `http://127.0.0.1:${process.env.E2E_MAIL_API_PORT || 8026}`;

export type CapturedMessage = { to: string[]; from: string; subject: string; text: string; received: number };

export async function messagesFor(email: string): Promise<CapturedMessage[]> {
  const response = await fetch(`${MAIL_API}/messages?to=${encodeURIComponent(email)}`);
  if (!response.ok) throw new Error(`mail capture service answered ${response.status}`);
  return (await response.json()) as CapturedMessage[];
}

export function tokenIn(message: CapturedMessage): string {
  const found = /\/reset-password#token=([A-Za-z0-9_-]+)/.exec(message.text);
  if (!found) throw new Error("the message contains no reset link");
  return found[1];
}

// Delivery happens after the HTTP response, so a message may arrive a moment later: wait for it.
export async function latestResetToken(email: string, timeoutMs = 10_000): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const all = await messagesFor(email);
    if (all.length) return tokenIn(all[all.length - 1]);
    if (Date.now() > deadline) throw new Error(`no reset message for ${email}`);
    await new Promise((r) => setTimeout(r, 150));
  }
}

// How many messages reached `email`. Delivery is asynchronous, so a count is only meaningful after waiting: `atLeast` waits
// for that many; for "none arrived" pass 0 and the call waits `settleMs` to give a wrongly-sent message time to show up.
export async function resetMessagesFor(email: string, atLeast = 0, settleMs = 1200): Promise<number> {
  const deadline = Date.now() + (atLeast ? 10_000 : settleMs);
  for (;;) {
    const count = (await messagesFor(email)).length;
    if (atLeast ? count >= atLeast : Date.now() > deadline) return count;
    if (atLeast && Date.now() > deadline) return count;
    await new Promise((r) => setTimeout(r, 150));
  }
}
