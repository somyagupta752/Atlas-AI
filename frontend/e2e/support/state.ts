import fs from "node:fs";
import path from "node:path";

export interface E2EState {
  database: string;
  database_url: string;
  pg_dsn: string;
  fixture_root: string;
  fixture_repo: string;
  auth_secret: string;
  log: string;
  api_port: number;
}

const STATE_FILE = path.resolve(import.meta.dirname, "..", ".state", "state.json");

// Written by run_api.py once the throwaway database exists. Read lazily: the file appears after Playwright starts the server.
export function readState(): E2EState {
  return JSON.parse(fs.readFileSync(STATE_FILE, "utf8")) as E2EState;
}
