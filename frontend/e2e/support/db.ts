import { Client } from "pg";
import { readState } from "./state";

// Direct SQL against the THROWAWAY database only (its name must start with atlas_e2e_). Used to age a token so a test
// need not wait out a real time window, and to clean up and verify cleanup.
async function withClient<T>(run: (client: Client) => Promise<T>): Promise<T> {
  const state = readState();
  if (!state.database.startsWith("atlas_e2e_")) throw new Error(`refusing to run SQL against ${state.database}`);
  const client = new Client({ connectionString: state.pg_dsn });
  await client.connect();
  try {
    return await run(client);
  } finally {
    await client.end();
  }
}

export const E2E_EMAIL_DOMAIN = "e2e.example.test";

// Make every spent refresh token look as if it was spent a while ago, ending the 10-second concurrent-refresh grace window.
export async function ageSpentRefreshTokens(email: string): Promise<void> {
  await withClient((c) =>
    c.query(
      `UPDATE refresh_tokens SET revoked_at = revoked_at - interval '5 minutes'
       WHERE revoked_at IS NOT NULL AND user_id = (SELECT id FROM users WHERE email = $1)`,
      [email.toLowerCase()],
    ),
  );
}

// Make a reset link look as if it was issued long ago, so "expired" can be tested without waiting 30 minutes.
export async function expireResetTokens(email: string): Promise<void> {
  await withClient((c) =>
    c.query(
      `UPDATE password_reset_tokens SET expires_at = now() - interval '1 minute'
       WHERE user_id = (SELECT id FROM users WHERE email = $1)`,
      [email.toLowerCase()],
    ),
  );
}

export async function resetTokenHashes(email: string): Promise<string[]> {
  return withClient(async (c) => {
    const { rows } = await c.query(
      "SELECT token_hash FROM password_reset_tokens WHERE user_id = (SELECT id FROM users WHERE email = $1)", [email.toLowerCase()]);
    return rows.map((r) => r.token_hash as string);
  });
}

export async function refreshTokenCount(email: string): Promise<{ live: number; revoked: number }> {
  return withClient(async (c) => {
    const { rows } = await c.query(
      `SELECT count(*) FILTER (WHERE revoked_at IS NULL) AS live, count(*) FILTER (WHERE revoked_at IS NOT NULL) AS revoked
       FROM refresh_tokens WHERE user_id = (SELECT id FROM users WHERE email = $1)`,
      [email.toLowerCase()],
    );
    return { live: Number(rows[0].live), revoked: Number(rows[0].revoked) };
  });
}

// Delete every account and repository the tests created, then say what is left. Called after the run; the database itself is
// also dropped when the API server stops, so this is a second, checked line of defence rather than the only one.
export async function cleanUp(): Promise<Record<string, number>> {
  return withClient(async (c) => {
    // Evaluation history is immutable in production (database triggers refuse UPDATE and DELETE). In this throwaway database,
    // and only here (withClient refuses any name that is not atlas_e2e_*), the cleanup session turns those triggers off so the
    // accounts the tests created can be removed along with everything that references them.
    await c.query("SET session_replication_role = replica");
    const owned = "(SELECT id FROM users WHERE email LIKE $1)";
    const like = [`%@${E2E_EMAIL_DOMAIN}`];
    for (const table of ["evaluation_baselines", "evaluation_runs", "evaluation_configurations"]) {
      await c.query(`DELETE FROM ${table} WHERE owner_id IN ${owned}`, like);
    }
    // With triggers off the database's ON DELETE CASCADE does not run either, so dependents are removed explicitly.
    await c.query(`DELETE FROM refresh_tokens WHERE user_id IN ${owned}`, like);
    await c.query(`DELETE FROM password_reset_tokens WHERE user_id IN ${owned}`, like);
    await c.query("DELETE FROM repository_memberships");
    await c.query("DELETE FROM repositories");
    await c.query("DELETE FROM code_chunks");
    await c.query("DELETE FROM repository_indexes");
    await c.query("DELETE FROM users WHERE email LIKE $1", like);
    await c.query("SET session_replication_role = DEFAULT");
    const tables = ["users", "repositories", "repository_memberships", "refresh_tokens", "password_reset_tokens",
      "repository_indexes", "code_chunks", "evaluation_configurations", "evaluation_runs", "evaluation_baselines"];
    const left: Record<string, number> = {};
    for (const table of tables) left[table] = Number((await c.query(`SELECT count(*) AS n FROM ${table}`)).rows[0].n);
    return left;
  });
}
