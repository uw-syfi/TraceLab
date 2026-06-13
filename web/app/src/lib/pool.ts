// Contributed-pool dashboard data. The shape matches the server's GET /api/pool snapshot
// (web/server/store.py :: pool_preview). Pool.astro renders this zeroed default at build time and
// hydrates it client-side from /api/pool, so the static site stays static and the numbers stay
// live. Timestamps are ISO; the client formats relative ("2h ago") at fetch time.

export interface PoolContribution {
  id: string;
  receivedAt: string | null;
  rows: number;
  providers: Array<'claude' | 'codex'>;
  status: 'validated';
}

export interface PoolPreview {
  contributors: number;
  rounds: number;
  totalInputTokens: number;
  lastContributionAt: string | null;
  split: { claude: number; codex: number };
  contributions: PoolContribution[];
}

/** Empty skeleton used for the build-time render before the client hydrates from /api/pool. */
export const poolEmpty: PoolPreview = {
  contributors: 0,
  rounds: 0,
  totalInputTokens: 0,
  lastContributionAt: null,
  split: { claude: 0, codex: 0 },
  contributions: [],
};
