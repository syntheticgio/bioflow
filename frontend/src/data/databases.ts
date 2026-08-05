import type { DbEntry } from "./DbEntry";

// Vite + tsconfig resolveJsonModule: true — JSON imports are handled natively.
import databasesJson from "./databases.json";

export type { DbEntry };
export const DATABASES: DbEntry[] = (databasesJson as unknown) as DbEntry[];
