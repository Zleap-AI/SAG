import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { pool, closePool } from "./pool.js";
import { logger } from "../observability/logger.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "../..");
const migrationsDir = path.join(rootDir, "migrations");

export async function migrate(): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("begin");
    await client.query(`
      create table if not exists schema_migrations (
        name text primary key,
        applied_at timestamptz not null default now()
      )
    `);
    await client.query("commit");

    const files = (await fs.readdir(migrationsDir))
      .filter((name) => name.endsWith(".sql"))
      .sort();

    for (const file of files) {
      const alreadyApplied = await client.query(
        "select 1 from schema_migrations where name = $1",
        [file]
      );
      if (alreadyApplied.rowCount && alreadyApplied.rowCount > 0) {
        logger.info({ migration: file }, "migration already applied");
        continue;
      }

      const sql = await fs.readFile(path.join(migrationsDir, file), "utf8");
      await client.query("begin");
      try {
        await client.query(sql);
        await client.query("insert into schema_migrations (name) values ($1)", [file]);
        await client.query("commit");
        logger.info({ migration: file }, "migration applied");
      } catch (error) {
        await client.query("rollback");
        throw error;
      }
    }
  } finally {
    client.release();
  }
}

const isMainModule = process.argv[1] && path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1]);
if (isMainModule) {
  migrate()
    .then(async () => closePool())
    .catch(async (error: unknown) => {
      logger.error({ error }, "migration failed");
      await closePool();
      process.exit(1);
    });
}

