import path from "node:path";
import { fileURLToPath } from "node:url";
import { pool, closePool } from "./pool.js";
import { logger } from "../observability/logger.js";

export const defaultEntityTypes = [
  {
    id: "30000000-0000-0000-0000-000000000001",
    type: "time",
    name: "time",
    description: "Time points, periods, dates, years and temporal expressions.",
    weight: 1.0,
    similarityThreshold: 0.9
  },
  {
    id: "30000000-0000-0000-0000-000000000002",
    type: "location",
    name: "location",
    description: "Countries, cities, regions, places and physical locations.",
    weight: 1.0,
    similarityThreshold: 0.75
  },
  {
    id: "30000000-0000-0000-0000-000000000003",
    type: "person",
    name: "person",
    description: "People and named individuals.",
    weight: 1.2,
    similarityThreshold: 0.8
  },
  {
    id: "30000000-0000-0000-0000-000000000004",
    type: "organization",
    name: "organization",
    description: "Companies, institutions, teams and organizations.",
    weight: 1.1,
    similarityThreshold: 0.8
  },
  {
    id: "30000000-0000-0000-0000-000000000005",
    type: "subject",
    name: "subject",
    description: "Main topics, concepts and subjects.",
    weight: 1.5,
    similarityThreshold: 0.78
  },
  {
    id: "30000000-0000-0000-0000-000000000006",
    type: "product",
    name: "product",
    description: "Products, services, projects and named offerings.",
    weight: 1.1,
    similarityThreshold: 0.8
  },
  {
    id: "30000000-0000-0000-0000-000000000007",
    type: "metric",
    name: "metric",
    description: "Numbers, metrics, measurements, amounts and statistics.",
    weight: 1.2,
    similarityThreshold: 0.85
  },
  {
    id: "30000000-0000-0000-0000-000000000008",
    type: "action",
    name: "action",
    description: "Important actions, changes, decisions and operations.",
    weight: 1.3,
    similarityThreshold: 0.78
  },
  {
    id: "30000000-0000-0000-0000-000000000009",
    type: "work",
    name: "work",
    description: "Creative works, documents, papers, books, films and reports.",
    weight: 1.0,
    similarityThreshold: 0.8
  },
  {
    id: "30000000-0000-0000-0000-000000000010",
    type: "group",
    name: "group",
    description: "Groups, communities, audiences and populations.",
    weight: 1.0,
    similarityThreshold: 0.78
  },
  {
    id: "30000000-0000-0000-0000-000000000011",
    type: "tags",
    name: "tags",
    description: "Fallback labels when no specific entity type fits.",
    weight: 0.5,
    similarityThreshold: 0.7
  }
] as const;

export async function seed(): Promise<void> {
  for (const item of defaultEntityTypes) {
    await pool.query(
      `
        insert into entity_types (
          id, scope, type, name, description, weight, similarity_threshold, is_default, is_active
        )
        values ($1, 'global', $2, $3, $4, $5, $6, true, true)
        on conflict (id) do update set
          type = excluded.type,
          name = excluded.name,
          description = excluded.description,
          weight = excluded.weight,
          similarity_threshold = excluded.similarity_threshold,
          is_default = true,
          is_active = true,
          updated_at = now()
      `,
      [
        item.id,
        item.type,
        item.name,
        item.description,
        item.weight,
        item.similarityThreshold
      ]
    );
  }

  logger.info({ count: defaultEntityTypes.length }, "seed complete");
}

const isMainModule = process.argv[1] && path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1]);
if (isMainModule) {
  seed()
    .then(async () => closePool())
    .catch(async (error: unknown) => {
      logger.error({ error }, "seed failed");
      await closePool();
      process.exit(1);
    });
}
