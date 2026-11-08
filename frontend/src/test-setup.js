import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { setToken } from "./auth/session";

beforeEach(() => setToken(null));
afterEach(() => cleanup());
