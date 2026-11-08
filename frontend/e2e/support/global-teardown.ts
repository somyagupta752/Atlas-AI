import { cleanUp } from "./db";

// After the last test: remove what the tests created and FAIL the run if anything of theirs is left behind. (The throwaway
// database is dropped when the API server is stopped, right after this.)
export default async function globalTeardown(): Promise<void> {
  const left = await cleanUp();
  if (Object.values(left).some((n) => n !== 0)) {
    throw new Error(`E2E clean-up left data behind: ${JSON.stringify(left)}`);
  }
}
